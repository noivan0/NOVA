"""
nova.engine.memory_slim — Memory Slim Engine: trim the agent memory file.

Called by BrainWatcher when the memory file exceeds 85% of the size limit.
30-min cooldown.

The memory file ($NOVA_HOME/memory.md) is a persistent markdown file that
accumulates notes across sessions. This engine trims it to stay under the
20,000 character limit by:
  1. Splitting on section separators (§ or ---)
  2. Keeping the most recently added sections
  3. Archiving trimmed sections to $NOVA_HOME/logs/memory_archive.md
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

MEMORY_LIMIT = 20_000    # characters
TARGET_PCT   = 0.75      # aim for 75% after slim
SEP          = "§"       # section separator used by NOVA memory format


def run(nova_home: Path, limit: int = MEMORY_LIMIT) -> bool:
    """Slim the memory file if it exceeds 85% of limit. Returns True if slimmed."""
    memory_md = nova_home / "memory.md"

    if not memory_md.exists():
        print("[memory_slim] memory.md not found — skip")
        return False

    text = memory_md.read_text(encoding="utf-8")
    chars = len(text)
    pct = int(chars * 100 / limit)

    if pct < 85:
        print(f"[memory_slim] {pct}% — below threshold, skip")
        return False

    # Split into sections
    sections = [s.strip() for s in text.split(SEP) if s.strip()]
    if not sections:
        print("[memory_slim] no sections found — skip")
        return False

    # Archive older sections, keep recent ones up to target
    target_chars = int(limit * TARGET_PCT)
    kept: list[str] = []
    archived: list[str] = []

    for section in reversed(sections):
        if sum(len(s) for s in kept) + len(section) < target_chars:
            kept.insert(0, section)
        else:
            archived.insert(0, section)

    if not archived:
        print(f"[memory_slim] {pct}% but cannot trim further (single large section)")
        return False

    # Write trimmed memory
    new_text = ("\n" + SEP + "\n").join(kept)
    if not new_text.endswith("\n"):
        new_text += "\n"
    memory_md.write_text(new_text, encoding="utf-8")

    # Append archived sections to archive log
    archive_log = nova_home / "logs" / "memory_archive.md"
    archive_log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(archive_log, "a", encoding="utf-8") as f:
        f.write(f"\n\n## Archived {ts} (was {pct}%)\n\n")
        f.write(("\n" + SEP + "\n").join(archived))
        f.write("\n")

    new_chars = len(memory_md.read_text(encoding="utf-8"))
    new_pct = int(new_chars * 100 / limit)
    print(f"[memory_slim] {pct}% → {new_pct}% (archived {len(archived)} sections)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Memory Slim Engine")
    parser.add_argument("--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"))
    parser.add_argument("--force", action="store_true", help="Run even if below threshold")
    args = parser.parse_args()
    nova_home = Path(args.nova_home).expanduser().resolve()

    if args.force:
        # Temporarily pretend limit is 0 to force a slim
        text = (nova_home / "memory.md").read_text() if (nova_home / "memory.md").exists() else ""
        if text:
            run(nova_home, limit=len(text) + 1)
    else:
        run(nova_home)


if __name__ == "__main__":
    main()
