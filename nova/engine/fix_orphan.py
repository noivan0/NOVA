"""
nova.engine.fix_orphan — Fix Orphan Pages: assign agents to unowned KB pages.

Called by BrainWatcher when orphan page count >= 3. 30-second cooldown.

An "orphan" is a brain.db page with no agent association (agent IS NULL).
This engine heuristically assigns agents based on file path patterns.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path


# Path-pattern → agent mapping (customise to match your agent names)
_AGENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"lessons/"), "nova-learn"),
    (re.compile(r"agents/nova-learn"), "nova-learn"),
    (re.compile(r"agents/nova-document"), "nova-document"),
    (re.compile(r"agents/nova-research"), "nova-research"),
    (re.compile(r"skills/"), "nova-skill"),
    (re.compile(r"synthesis/"), "nova-synthesize"),
    (re.compile(r"wiki/"), "nova-wiki"),
    (re.compile(r"kb/"), "nova"),
]


def _infer_agent(path: str) -> str:
    for pattern, agent in _AGENT_PATTERNS:
        if pattern.search(path):
            return agent
    return "nova"


def run(nova_home: Path) -> int:
    """Fix orphan pages. Returns count of pages fixed."""
    brain_db = nova_home / "brain.db"

    if not brain_db.exists():
        print("[fix_orphan] brain.db not found — skip")
        return 0

    db = sqlite3.connect(str(brain_db), timeout=5)
    db.row_factory = sqlite3.Row

    try:
        orphans = db.execute(
            "SELECT id, path FROM pages WHERE agent IS NULL AND page_type='general' LIMIT 50"
        ).fetchall()
    except sqlite3.OperationalError:
        db.close()
        print("[fix_orphan] pages table not ready — skip")
        return 0

    fixed = 0
    for page in orphans:
        agent = _infer_agent(page["path"])
        db.execute("UPDATE pages SET agent=? WHERE id=?", (agent, page["id"]))
        fixed += 1

    db.commit()
    db.close()
    print(f"[fix_orphan] fixed {fixed} orphan pages")
    return fixed


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Fix Orphan Pages Engine")
    parser.add_argument("--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"))
    args = parser.parse_args()
    nova_home = Path(args.nova_home).expanduser().resolve()
    run(nova_home)


if __name__ == "__main__":
    main()
