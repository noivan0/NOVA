"""
nova.engine.dream — DreamCycle Engine: deep consolidation pass.

Called by BrainWatcher when:
  - brain.db accumulates +100 takes (major knowledge milestone), OR
  - health score drops below 90

Heaviest engine, 2-hour cooldown.

What it does:
  1. Compute brain health score (orphan ratio, contradiction density)
  2. Record a brain_health snapshot
  3. Prune stale/low-weight takes (optional, disabled by default)
  4. Emit DREAM_DONE event
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_health(db: sqlite3.Connection) -> float:
    """Compute a 0–100 health score from database metrics."""
    takes = db.execute("SELECT count(*) FROM takes").fetchone()[0]
    pages = db.execute("SELECT count(*) FROM pages").fetchone()[0]

    # Orphan ratio: unlinked takes hurt health
    orphan_takes = db.execute("SELECT count(*) FROM takes WHERE page_id IS NULL").fetchone()[0]
    orphan_pages = db.execute(
        "SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'"
    ).fetchone()[0]

    # Contradiction density
    try:
        open_contra = db.execute(
            "SELECT count(*) FROM contradictions WHERE status='open'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        open_contra = 0

    orphan_ratio = orphan_takes / max(takes, 1)
    contra_penalty = min(open_contra * 2, 20)
    page_orphan_penalty = min(orphan_pages / max(pages, 1) * 20, 15)

    score = 100.0 - (orphan_ratio * 40) - contra_penalty - page_orphan_penalty
    return max(0.0, min(100.0, round(score, 1)))


def run(nova_home: Path, prune: bool = False) -> float:
    """Run DreamCycle. Returns health score."""
    brain_db = nova_home / "brain.db"

    if not brain_db.exists():
        print("[dream] brain.db not found — skip")
        return 100.0

    db = sqlite3.connect(str(brain_db), timeout=15)
    db.row_factory = sqlite3.Row

    health = _compute_health(db)

    takes = db.execute("SELECT count(*) FROM takes").fetchone()[0]
    orphan_takes = db.execute("SELECT count(*) FROM takes WHERE page_id IS NULL").fetchone()[0]

    # Optional: prune very old low-weight takes (keeps DB lean)
    pruned = 0
    if prune:
        result = db.execute(
            "DELETE FROM takes WHERE weight < 0.2 AND "
            "created_at < datetime('now', '-30 days') AND page_id IS NULL"
        )
        pruned = result.rowcount

    # Record health snapshot
    hid = uuid.uuid4().hex[:16]
    try:
        db.execute(
            "INSERT OR IGNORE INTO brain_health "
            "(id, score_overall, takes_total, orphan_count, created_at) VALUES (?,?,?,?,?)",
            (hid, health, takes, orphan_takes, _now()),
        )
    except sqlite3.OperationalError:
        # brain_health table may not exist in minimal installs — create it
        db.execute(
            "CREATE TABLE IF NOT EXISTS brain_health "
            "(id TEXT PRIMARY KEY, score_overall REAL, takes_total INTEGER, "
            "orphan_count INTEGER, created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO brain_health VALUES (?,?,?,?,?)",
            (hid, health, takes, orphan_takes, _now()),
        )

    # Emit event
    eid = uuid.uuid4().hex[:16]
    try:
        db.execute(
            "INSERT INTO nova_events "
            "(id,event_type,severity,title,detail,source,created_at,is_read) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "DREAM_DONE", "INFO",
             f"DreamCycle complete — health={health}",
             f"takes={takes} orphan={orphan_takes} pruned={pruned}",
             "nova.engine.dream", _now(), 0),
        )
    except sqlite3.OperationalError:
        pass

    db.commit()
    db.close()

    print(f"[dream] health={health:.1f} takes={takes} orphan={orphan_takes} pruned={pruned}")
    return health


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA DreamCycle Engine")
    parser.add_argument("--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"))
    parser.add_argument("--prune", action="store_true", help="Prune stale low-weight takes")
    args = parser.parse_args()
    nova_home = Path(args.nova_home).expanduser().resolve()
    nova_home.mkdir(parents=True, exist_ok=True)
    run(nova_home, prune=args.prune)


if __name__ == "__main__":
    main()
