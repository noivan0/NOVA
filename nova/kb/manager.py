"""
nova.kb.manager — KB page read/write with frontmatter validation.

Follows the Agent KB Pattern:
  https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18
"""

from __future__ import annotations

import re
import hashlib
from datetime import date
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


VALID_TYPES = {"config", "fix", "project", "user", "concept", "weekly", "comparison"}
VALID_STATUSES = {"active", "resolved", "archived"}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class KBPage:
    """A parsed KB page with frontmatter and body."""

    def __init__(self, path: Path, frontmatter: dict[str, Any], body: str):
        self.path = path
        self.fm = frontmatter
        self.body = body

    @property
    def title(self) -> str:
        return self.fm.get("title", self.path.stem)

    @property
    def page_type(self) -> str:
        return self.fm.get("type", "concept")

    @property
    def tags(self) -> list[str]:
        return self.fm.get("tags", [])

    @property
    def status(self) -> str:
        return self.fm.get("status", "active")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"KBPage({self.path.name!r}, type={self.page_type!r}, status={self.status!r})"


class KBManager:
    """
    Read and write KB pages with frontmatter validation.

    Usage::

        from nova.kb import KBManager

        kb = KBManager("~/.agent/kb")

        # Write a new fix page
        kb.write(
            name="ssl-r2dbc-error",
            page_type="fix",
            tags=["ssl", "gateway"],
            status="resolved",
            body="## Root Cause\\nMissing x-api-key header...\\n\\n## Fix\\n...",
        )

        # Read it back
        page = kb.read("ssl-r2dbc-error")
        print(page.title, page.status)

        # Append to log
        kb.log("fix | ssl-r2dbc-error — resolved")
    """

    def __init__(self, kb_root: str | Path):
        self.root = Path(kb_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "log.md").touch(exist_ok=True)
        (self.root / "index.md").touch(exist_ok=True)

    def _safe_page_path(self, name: str, subdir: str = "") -> Path:
        """Resolve (name, subdir) to a .md path guaranteed to be under self.root.

        SECURITY-007 (2026-08-18, Codex-audited deep audit): write()/read()/
        pages() composed `self.root / subdir / f"{name}.md"` with no
        traversal check at all. Codex reproduced a real file written
        OUTSIDE self.root via write(name="../escaped", ...). Both `name`
        and `subdir` are rejected outright if they contain '..' or are
        absolute, then the final resolved path is hard-verified to be a
        descendant of self.root (relative_to() check) — same
        defense-in-depth pattern as nova.core.kb.KB._resolve() and
        nova.kernel.syscall.KernelAPI._validate_path().
        """
        for label, value in (("name", name), ("subdir", subdir)):
            if not value:
                continue
            if ".." in Path(value).parts or Path(value).is_absolute():
                raise ValueError(f"KB {label} must not contain '..' or be absolute: {value!r}")

        target_dir = (self.root / subdir) if subdir else self.root
        candidate = (target_dir / f"{name}.md").resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(
                f"KB page resolves outside the KB root, refusing: "
                f"name={name!r} subdir={subdir!r} -> {candidate}"
            )
        return candidate

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def read(self, name: str, subdir: str = "") -> Optional[KBPage]:
        """Read a KB page by name (without .md extension)."""
        candidates = [self._safe_page_path(name, subdir)]
        if subdir:
            candidates.append(self._safe_page_path(name))
        for path in candidates:
            if path.exists():
                return self._parse(path)
        return None

    def read_path(self, path: Path | str) -> Optional[KBPage]:
        path = Path(path).expanduser().resolve()
        return self._parse(path) if path.exists() else None

    def pages(self, subdir: str = "", status_filter: str = "") -> list[KBPage]:
        """List all KB pages, optionally filtered by subdir and status."""
        if subdir and (".." in Path(subdir).parts or Path(subdir).is_absolute()):
            raise ValueError(f"KB subdir must not contain '..' or be absolute: {subdir!r}")
        search_root = (self.root / subdir).resolve() if subdir else self.root
        try:
            search_root.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(f"KB subdir resolves outside the KB root: {subdir!r}")
        results = []
        for md in search_root.rglob("*.md"):
            if md.name in {"SCHEMA.md", "index.md", "log.md"}:
                continue
            page = self._parse(md)
            if page and (not status_filter or page.status == status_filter):
                results.append(page)
        return sorted(results, key=lambda p: p.fm.get("updated", ""), reverse=True)

    def active_projects(self) -> list[KBPage]:
        """Return project pages with status=active."""
        return self.pages(subdir="projects", status_filter="active")

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def write(
        self,
        name: str,
        page_type: str,
        body: str,
        tags: list[str] | None = None,
        status: str = "active",
        subdir: str = "",
        title: str = "",
        extra_fm: dict | None = None,
    ) -> KBPage:
        """Create or overwrite a KB page."""
        if page_type not in VALID_TYPES:
            raise ValueError(f"Invalid page_type {page_type!r}. Must be one of {VALID_TYPES}")
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}. Must be one of {VALID_STATUSES}")

        path = self._safe_page_path(name, subdir)
        path.parent.mkdir(parents=True, exist_ok=True)

        today = date.today().isoformat()
        fm: dict[str, Any] = {
            "title": title or name.replace("-", " ").title(),
            "created": today,
            "updated": today,
            "type": page_type,
            "tags": tags or [],
            "status": status,
        }
        if extra_fm:
            fm.update(extra_fm)

        # Preserve created date on update
        existing = self.read_path(path)
        if existing:
            fm["created"] = existing.fm.get("created", today)

        content = self._render(fm, body)
        path.write_text(content)

        self._update_index(name, fm["title"], page_type, subdir)
        self.log(f"{'update' if existing else 'create'} | {name}")

        return KBPage(path, fm, body)

    def append(self, name: str, section: str, content: str, subdir: str = "") -> None:
        """Append a new section to an existing KB page."""
        page = self.read(name, subdir)
        if not page:
            raise FileNotFoundError(f"KB page {name!r} not found")

        new_body = page.body.rstrip() + f"\n\n## {section}\n{content}\n"
        self.write(
            name=name,
            page_type=page.page_type,
            body=new_body,
            tags=page.tags,
            status=page.status,
            subdir=subdir,
            title=page.title,
        )

    def log(self, action: str) -> None:
        """Append a line to log.md."""
        today = date.today().isoformat()
        entry = f"\n## [{today}] {action}\n"
        with open(self.root / "log.md", "a") as f:
            f.write(entry)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _parse(self, path: Path) -> Optional[KBPage]:
        try:
            text = path.read_text()
        except OSError:
            return None

        m = _FRONTMATTER_RE.match(text)
        if m and _HAS_YAML:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
                body = text[m.end():]
                return KBPage(path, fm, body)
            except yaml.YAMLError:
                pass

        # Fallback: no frontmatter
        return KBPage(path, {}, text)

    def _render(self, fm: dict, body: str) -> str:
        if _HAS_YAML:
            header = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
            return f"---\n{header}---\n\n{body}"
        # Minimal fallback without pyyaml
        lines = ["---"]
        for k, v in fm.items():
            if isinstance(v, list):
                lines.append(f"{k}: [{', '.join(v)}]")
            else:
                lines.append(f"{k}: {v}")
        lines += ["---", "", body]
        return "\n".join(lines)

    def _update_index(self, name: str, title: str, page_type: str, subdir: str) -> None:
        index_path = self.root / "index.md"
        text = index_path.read_text() if index_path.exists() else "# KB Index\n"
        link = f"[[{subdir + '/' if subdir else ''}{name}]]"
        if link not in text:
            today = date.today().isoformat()
            entry = f"- {link} — {title}  <!-- {page_type} · {today} -->\n"
            text = text.rstrip() + "\n" + entry
            index_path.write_text(text)
