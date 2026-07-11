"""
nova/core/kb.py
---------------
Knowledge Base — a lightweight markdown-based persistent store.

Structure:
  kb/
    index.md          <- auto-updated table of contents
    log.md            <- append-only activity log
    config/           <- system configuration notes
    fixes/            <- bug fixes and workarounds
    projects/         <- per-project notes
    user/             <- user preferences and context

Usage:
  from nova.core.kb import KB
  kb = KB("./kb")
  kb.write("projects/my-harness", "# Notes\n\nSomething important.")
  kb.append_log("harness-run | my-harness — phase 3 complete")
  notes = kb.read("projects/my-harness")
  results = kb.search("keyword")
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


class KB:
    def __init__(self, path: str = "./kb"):
        self.root = Path(path).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_structure()

    # ------------------------------------------------------------------ #
    # Read / Write
    # ------------------------------------------------------------------ #

    def write(self, key: str, content: str) -> Path:
        """
        Write content to kb/<key>.md.
        Creates parent directories automatically.
        Returns the file path.
        """
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self._update_index(key)
        return p

    def append(self, key: str, content: str) -> None:
        """Append content to an existing KB page (creates if missing)."""
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write("\n" + content)
        self._update_index(key)

    def read(self, key: str) -> Optional[str]:
        """Read a KB page. Returns None if it doesn't exist."""
        p = self._resolve(key)
        if p.exists():
            return p.read_text()
        return None

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.exists():
            p.unlink()

    # ------------------------------------------------------------------ #
    # Log
    # ------------------------------------------------------------------ #

    def append_log(self, message: str) -> None:
        """Append a timestamped entry to kb/log.md."""
        log_path = self.root / "log.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(log_path, "a") as f:
            f.write(f"\n## [{ts}] {message}")

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def search(self, query: str, case_sensitive: bool = False) -> List[dict]:
        """
        Simple keyword search across all KB pages.
        Returns list of { key, path, line_number, line } dicts.
        """
        results = []
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(query), flags)

        for md_file in sorted(self.root.rglob("*.md")):
            key = self._to_key(md_file)
            for i, line in enumerate(md_file.read_text().splitlines(), start=1):
                if pattern.search(line):
                    results.append({
                        "key": key,
                        "path": str(md_file),
                        "line_number": i,
                        "line": line.strip(),
                    })

        return results

    def list_pages(self, prefix: str = "") -> List[str]:
        """List all KB page keys, optionally filtered by prefix."""
        pages = []
        for md_file in sorted(self.root.rglob("*.md")):
            key = self._to_key(md_file)
            if key.startswith(prefix):
                pages.append(key)
        return pages

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _resolve(self, key: str) -> Path:
        """Convert a KB key (e.g. 'projects/my-harness') to a file path."""
        # Sanitize: prevent path traversal
        key = key.lstrip("/").replace("..", "")
        if not key.endswith(".md"):
            key = key + ".md"
        return self.root / key

    def _to_key(self, path: Path) -> str:
        """Convert an absolute file path back to a KB key."""
        rel = path.relative_to(self.root)
        return str(rel).removesuffix(".md")

    def _ensure_structure(self) -> None:
        for subdir in ("config", "fixes", "projects", "user"):
            (self.root / subdir).mkdir(exist_ok=True)

        log_path = self.root / "log.md"
        if not log_path.exists():
            log_path.write_text("# KB Activity Log\n")

        index_path = self.root / "index.md"
        if not index_path.exists():
            index_path.write_text("# KB Index\n\n_Auto-generated. Do not edit manually._\n")

    def _update_index(self, key: str) -> None:
        """Ensure the key appears in index.md."""
        index_path = self.root / "index.md"
        content = index_path.read_text()
        link = f"- [[{key}]]"
        if link not in content:
            with open(index_path, "a") as f:
                f.write(f"\n{link}")
