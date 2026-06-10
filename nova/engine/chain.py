"""
nova.engine.chain — Chain Engine: process completed kanban tasks.

Called by BrainWatcher when kanban.db shows new "done" tasks.
10-second cooldown.

What it does:
  1. Read the most recently completed kanban task
  2. Check if it has a "next" dependency chain
  3. Promote next tasks to "ready" status
  4. Optionally: record the completion as a brain take
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def run(nova_home: Path) -> int:
    """Process completed tasks and promote next tasks. Returns count processed."""
    kanban_root = nova_home / "kanban" / "boards"

    if not kanban_root.exists():
        print("[chain] no kanban boards — skip")
        return 0

    brain_db = nova_home / "brain.db"
    processed = 0

    for board_dir in kanban_root.iterdir():
        db_path = board_dir / "kanban.db"
        if not db_path.exists():
            continue

        try:
            db = sqlite3.connect(str(db_path), timeout=5)
            db.row_factory = sqlite3.Row

            # Find recently completed tasks
            done_tasks = db.execute(
                "SELECT id, title FROM tasks WHERE status='done' "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()

            for task in done_tasks:
                # Check for dependent tasks blocked on this one
                try:
                    blocked = db.execute(
                        "SELECT t.id, t.title FROM tasks t "
                        "JOIN task_links l ON t.id = l.task_id "
                        "WHERE l.depends_on = ? AND t.status = 'todo'",
                        (task["id"],),
                    ).fetchall()

                    for dep in blocked:
                        # Check all dependencies are done
                        pending_deps = db.execute(
                            "SELECT count(*) FROM task_links tl "
                            "JOIN tasks t2 ON tl.depends_on = t2.id "
                            "WHERE tl.task_id = ? AND t2.status != 'done'",
                            (dep["id"],),
                        ).fetchone()[0]

                        if pending_deps == 0:
                            db.execute(
                                "UPDATE tasks SET status='ready' WHERE id=?",
                                (dep["id"],),
                            )
                            processed += 1
                            print(f"[chain] {board_dir.name}: promoted '{dep['title'][:50]}' → ready")

                except sqlite3.OperationalError:
                    pass  # task_links table may not exist

            db.commit()
            db.close()

            # Record completion as brain take (optional — requires brain.db)
            if brain_db.exists() and done_tasks:
                try:
                    bdb = sqlite3.connect(str(brain_db), timeout=5)
                    for task in done_tasks[:3]:  # max 3 per cycle
                        import uuid
                        from datetime import datetime, timezone
                        tid = uuid.uuid4().hex[:16]
                        now = datetime.now(timezone.utc).isoformat()
                        bdb.execute(
                            "INSERT OR IGNORE INTO takes "
                            "(id, kind, holder, claim, weight, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (tid, "fact", "nova-chain",
                             f"Task completed: {task['title'][:200]}",
                             0.5, now, now),
                        )
                    bdb.commit()
                    bdb.close()
                except Exception:
                    pass

        except Exception as e:
            print(f"[chain] error on board {board_dir.name}: {e}")

    print(f"[chain] processed {processed} task transitions")
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Chain Engine")
    parser.add_argument("--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"))
    args = parser.parse_args()
    nova_home = Path(args.nova_home).expanduser().resolve()
    run(nova_home)


if __name__ == "__main__":
    main()
