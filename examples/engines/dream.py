"""
examples/engines/dream.py — Minimal DreamCycle engine for BrainWatcher.

Triggered when brain.db accumulates +100 takes OR health drops below 90.

The DreamCycle is the heaviest engine — runs infrequently (2h cooldown).
It should consolidate knowledge, resolve contradictions, and record a
brain_health snapshot.

Usage:
    python examples/engines/dream.py --nova-home ~/.nova
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


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA DreamCycle Engine — minimal example")
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory",
    )
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    brain_db = nova_home / "brain.db"

    if not brain_db.exists():
        print("[dream] brain.db not found — nothing to do")
        return

    try:
        db = sqlite3.connect(str(brain_db), timeout=10)
        takes = db.execute("SELECT count(*) FROM takes").fetchone()[0]
        orphan = db.execute(
            "SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'"
        ).fetchone()[0]

        # Compute a simple health score (example: 100 - orphan_ratio * 50)
        pages = db.execute("SELECT count(*) FROM pages").fetchone()[0]
        health = 100.0 if pages == 0 else max(0.0, 100.0 - (orphan / max(pages, 1)) * 50)

        # Record health snapshot
        hid = uuid.uuid4().hex[:16]
        db.execute(
            "INSERT OR IGNORE INTO brain_health "
            "(id, score_overall, takes_total, orphan_count, created_at) VALUES (?,?,?,?,?)",
            (hid, health, takes, orphan, _now()),
        )
        db.commit()
        db.close()

        print(f"[dream] cycle complete — takes={takes}, health={health:.1f}, orphan={orphan}")
    except Exception as e:
        print(f"[dream] error: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
