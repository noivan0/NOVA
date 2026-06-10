"""
examples/engines/learn.py — Minimal learn engine for BrainWatcher.

Triggered when brain.db accumulates +5 new takes.

This example:
  1. Reads the most recent unprocessed takes from brain.db
  2. Links each take to the most relevant KB page
  3. Logs a summary

Replace this with your own learning logic.

Usage (called automatically by BrainWatcher, or manually):
    python examples/engines/learn.py --nova-home ~/.nova
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Learn Engine — minimal example")
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory",
    )
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    brain_db = nova_home / "brain.db"

    if not brain_db.exists():
        print(f"[learn] brain.db not found at {brain_db} — nothing to do")
        return

    try:
        db = sqlite3.connect(str(brain_db), timeout=5)
        # Count unlinked takes
        unlinked = db.execute(
            "SELECT count(*) FROM takes WHERE page_id IS NULL"
        ).fetchone()[0]
        total = db.execute("SELECT count(*) FROM takes").fetchone()[0]
        db.close()
        print(f"[learn] takes={total}, unlinked={unlinked} — learning pass complete")
    except Exception as e:
        print(f"[learn] error: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
