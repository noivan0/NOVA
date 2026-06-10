"""
nova.db.brain — BrainDB: SQLite-backed knowledge store for NOVA.

Usage::

    from nova.db.brain import BrainDB

    db = BrainDB("~/.nova/brain.db")
    db.init()

    # Record a knowledge take
    db.add_take(holder="nova-learn", claim="SSL certs expire after 90 days", weight=0.9)

    # Snapshot current state
    snap = db.snapshot()
    print(snap)  # {'takes': 42, 'orphan': 0, 'open_contra': 0, 'health': 98.5}

    # Emit a system event
    db.push_event("SYNTHESIS_DONE", "INFO", "Wiki synthesized 12 pages")
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nova.db.schema import BRAIN_SCHEMA


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrainDB:
    """Lightweight SQLite brain — knowledge pages, takes, events.

    All paths accept ``~`` expansion and environment variables.
    The database is created automatically on first use.
    """

    def __init__(self, path: str | Path = "~/.nova/brain.db") -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Create tables if they don't exist."""
        with self._connect() as db:
            db.executescript(BRAIN_SCHEMA)

    def _connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=timeout)

    # ── snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any] | None:
        """Return current brain state metrics (read-only, safe to call frequently)."""
        try:
            db = self._connect(timeout=2)
            c = db.cursor()
            takes = c.execute("SELECT count(*) FROM takes").fetchone()[0]
            orphan = c.execute(
                "SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'"
            ).fetchone()[0]
            open_c = c.execute(
                "SELECT count(*) FROM contradictions WHERE status='open'"
            ).fetchone()[0]
            row = c.execute(
                "SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            health = row[0] if row else 100.0
            db.close()
            return {"takes": takes, "orphan": orphan, "open_contra": open_c, "health": health}
        except Exception:
            return None

    # ── takes ────────────────────────────────────────────────────────────────

    def add_take(
        self,
        claim: str,
        holder: str = "nova",
        kind: str = "fact",
        weight: float = 0.5,
        page_id: str | None = None,
    ) -> str:
        """Insert a knowledge take. Returns its id."""
        tid = uuid.uuid4().hex[:16]
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, page_id, kind, holder, claim[:500], weight, now, now),
            )
        return tid

    def recent_takes(self, n: int = 50, holder: str | None = None) -> list[dict]:
        """Return the N most recent takes, optionally filtered by holder."""
        with self._connect() as db:
            if holder:
                rows = db.execute(
                    "SELECT id,holder,claim,weight,created_at FROM takes "
                    "WHERE holder=? ORDER BY created_at DESC LIMIT ?",
                    (holder, n),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id,holder,claim,weight,created_at FROM takes "
                    "ORDER BY created_at DESC LIMIT ?",
                    (n,),
                ).fetchall()
        return [
            {"id": r[0], "holder": r[1], "claim": r[2], "weight": r[3], "created_at": r[4]}
            for r in rows
        ]

    # ── events ───────────────────────────────────────────────────────────────

    def push_event(
        self,
        event_type: str,
        severity: str = "INFO",
        title: str = "",
        detail: str = "",
        source: str = "nova",
    ) -> None:
        """Emit a system event (used for inter-component signaling)."""
        eid = uuid.uuid4().hex[:16]
        now = _now()
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO nova_events "
                    "(id,event_type,severity,title,detail,source,created_at,is_read) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (eid, event_type, severity, title, detail, source, now, 0),
                )
        except Exception:
            pass  # events are best-effort

    def unread_events(self, limit: int = 20) -> list[dict]:
        """Return unread events, oldest first."""
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id,event_type,severity,title,detail,source,created_at "
                    "FROM nova_events WHERE is_read=0 ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()
                # Mark as read
                db.execute(
                    "UPDATE nova_events SET is_read=1 WHERE is_read=0"
                )
        except Exception:
            return []
        return [
            {
                "id": r[0], "event_type": r[1], "severity": r[2],
                "title": r[3], "detail": r[4], "source": r[5], "created_at": r[6],
            }
            for r in rows
        ]

    # ── health ───────────────────────────────────────────────────────────────

    def record_health(self, score: float) -> None:
        """Store a health snapshot."""
        snap = self.snapshot() or {}
        hid = uuid.uuid4().hex[:16]
        with self._connect() as db:
            db.execute(
                "INSERT INTO brain_health (id,score_overall,takes_total,orphan_count,created_at) "
                "VALUES (?,?,?,?,?)",
                (hid, score, snap.get("takes", 0), snap.get("orphan", 0), _now()),
            )
