"""
nova.wiki.synthesize — Wiki page synthesis: crosslink, stale refresh, takes summary.

Provides three synthesis phases that can be run independently or together:

  crosslink  — add backlinks between wiki pages that reference each other
  takes      — generate/refresh a "knowledge summary" page from brain.db takes
  stale      — re-generate wiki pages whose source KB files have changed
  lessons    — synthesize lesson pages into an index
  index      — rebuild the wiki index page

Usage::

    # Run all phases
    python -m nova.wiki.synthesize --phase all --nova-home ~/.nova

    # Run crosslink only (e.g. after a synthesize cycle)
    python -m nova.wiki.synthesize --phase crosslink --nova-home ~/.nova

    # Dry run (no writes)
    python -m nova.wiki.synthesize --phase stale --dry-run

Customisation
-------------
By default, this script expects:
  - wiki pages in  $NOVA_HOME/wiki/
  - KB files in    $NOVA_HOME/kb/
  - brain DB at    $NOVA_HOME/brain.db

Override via --nova-home or NOVA_HOME environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable


# ── helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_wiki_links(text: str) -> set[str]:
    """Return all [[page-name]] links found in text."""
    return set(re.findall(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", text))


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[wiki-synth] [{ts}] {msg}", flush=True)


# ── crosslink ─────────────────────────────────────────────────────────────────

def phase_crosslink(wiki_root: Path, dry_run: bool = False) -> int:
    """Add backlinks to wiki pages that are referenced by other pages.

    Returns the count of pages that were updated.
    """
    if not wiki_root.exists():
        _log(f"wiki root not found: {wiki_root}")
        return 0

    pages = list(wiki_root.rglob("*.md"))
    _log(f"crosslink: scanning {len(pages)} pages")

    # Build forward link map: slug → set of slugs it links to
    slug_map: dict[str, Path] = {p.stem: p for p in pages}
    forward: dict[str, set[str]] = {}
    for p in pages:
        try:
            content = p.read_text(encoding="utf-8")
            forward[p.stem] = {
                s for s in _extract_wiki_links(content)
                if s in slug_map and s != p.stem
            }
        except OSError:
            forward[p.stem] = set()

    # Build reverse map: slug → set of slugs that link to it
    reverse: dict[str, set[str]] = {slug: set() for slug in slug_map}
    for src, targets in forward.items():
        for tgt in targets:
            reverse[tgt].add(src)

    updated = 0
    for slug, backlink_slugs in reverse.items():
        if not backlink_slugs:
            continue
        path = slug_map[slug]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue

        backlink_section = "\n\n## Backlinks\n" + "\n".join(
            f"- [[{s}]]" for s in sorted(backlink_slugs)
        ) + "\n"

        if "## Backlinks" in content:
            new_content = re.sub(
                r"\n\n## Backlinks\n.*",
                backlink_section,
                content,
                flags=re.DOTALL,
            )
        else:
            new_content = content.rstrip() + backlink_section

        if new_content != content:
            if not dry_run:
                path.write_text(new_content, encoding="utf-8")
            updated += 1

    _log(f"crosslink: updated {updated} pages")
    return updated


# ── takes summary ─────────────────────────────────────────────────────────────

def phase_takes(
    wiki_root: Path,
    brain_db: Path,
    llm_fn: Callable[[str], str] | None = None,
    dry_run: bool = False,
) -> int:
    """Summarise recent brain takes into a wiki entity page.

    If llm_fn is provided, it's called with a prompt and should return
    the generated markdown body.  Otherwise a simple aggregation is written.
    """
    if not brain_db.exists():
        _log(f"brain.db not found: {brain_db}")
        return 0

    try:
        db = sqlite3.connect(str(brain_db), timeout=3)
        rows = db.execute(
            "SELECT claim, weight FROM takes ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        db.close()
    except Exception as e:
        _log(f"takes: db error: {e}")
        return 0

    if not rows:
        _log("takes: no takes found")
        return 0

    takes_text = "\n".join(f"- {r[0][:200]} (weight={r[1]:.2f})" for r in rows)

    if llm_fn:
        prompt = (
            "Summarise the following knowledge takes into a wiki entity page.\n"
            "Format: markdown with sections by topic.\n\n"
            f"Takes:\n{takes_text}"
        )
        body = llm_fn(prompt)
    else:
        body = f"## Recent Knowledge Takes\n\n{takes_text}\n"

    entities_dir = wiki_root / "entities"
    if not dry_run:
        entities_dir.mkdir(parents=True, exist_ok=True)
        out = entities_dir / "nova-brain-takes-summary.md"
        out.write_text(
            f"---\ntitle: Brain Takes Summary\nupdated: {_today()}\n---\n\n{body}",
            encoding="utf-8",
        )
        _log(f"takes: wrote {len(rows)} takes → {out}")
    else:
        _log(f"takes: dry-run, would write {len(rows)} takes")

    return 1


# ── stale refresh ─────────────────────────────────────────────────────────────

def phase_stale(
    wiki_root: Path,
    kb_root: Path,
    max_age_days: int = 90,
    llm_fn: Callable[[str], str] | None = None,
    dry_run: bool = False,
) -> int:
    """Re-generate wiki pages whose source KB files are newer than the wiki page.

    A wiki page is considered stale if:
      - It has a ``source:`` frontmatter field pointing to a KB file, AND
      - The KB file mtime > wiki page mtime, OR the wiki page is > max_age_days old.
    """
    if not wiki_root.exists():
        return 0

    stale_pattern = re.compile(r"^source:\s+(.+)$", re.MULTILINE)
    updated = 0

    for wiki_page in wiki_root.rglob("*.md"):
        try:
            content = wiki_page.read_text(encoding="utf-8")
        except OSError:
            continue

        match = stale_pattern.search(content)
        if not match:
            continue

        source_path = kb_root / match.group(1).strip()
        if not source_path.exists():
            continue

        page_mtime = wiki_page.stat().st_mtime
        source_mtime = source_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - page_mtime) / 86400

        if source_mtime <= page_mtime and age_days < max_age_days:
            continue

        _log(f"stale: {wiki_page.name} (age={age_days:.0f}d, source newer={source_mtime > page_mtime})")

        if llm_fn:
            try:
                source_text = source_path.read_text(encoding="utf-8")
                new_body = llm_fn(
                    f"Refresh this wiki page based on the updated source document.\n\n"
                    f"Source:\n{source_text[:3000]}\n\nCurrent page:\n{content[:1000]}"
                )
                if not dry_run:
                    wiki_page.write_text(new_body, encoding="utf-8")
                updated += 1
            except Exception as e:
                _log(f"stale: error refreshing {wiki_page.name}: {e}")

    _log(f"stale: refreshed {updated} pages")
    return updated


# ── lessons index ─────────────────────────────────────────────────────────────

def phase_lessons(wiki_root: Path, kb_root: Path, dry_run: bool = False) -> int:
    """Synthesise lesson KB pages into a wiki lessons index."""
    lessons_dir = kb_root / "lessons"
    if not lessons_dir.exists():
        return 0

    lessons = sorted(lessons_dir.glob("*.md"))
    entries: list[str] = []
    for lesson in lessons:
        try:
            text = lesson.read_text(encoding="utf-8")
            first_line = next(
                (l.lstrip("#").strip() for l in text.splitlines() if l.strip()), lesson.stem
            )
            entries.append(f"- [[{lesson.stem}]] — {first_line[:100]}")
        except OSError:
            continue

    if not entries:
        return 0

    index_text = (
        f"---\ntitle: Lessons Index\nupdated: {_today()}\n---\n\n"
        "# Lessons\n\n" + "\n".join(entries) + "\n"
    )

    if not dry_run:
        out = wiki_root / "concepts" / "lessons-index.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(index_text, encoding="utf-8")
        _log(f"lessons: wrote {len(entries)} lessons → {out}")
    else:
        _log(f"lessons: dry-run, {len(entries)} lessons found")

    return len(entries)


# ── wiki index ────────────────────────────────────────────────────────────────

def phase_index(wiki_root: Path, dry_run: bool = False) -> int:
    """Rebuild the wiki index page from all wiki entities and concepts."""
    if not wiki_root.exists():
        return 0

    sections: dict[str, list[str]] = {}
    for page in wiki_root.rglob("*.md"):
        if page.name == "index.md":
            continue
        try:
            content = page.read_text(encoding="utf-8")
            first = next((l.lstrip("#").strip() for l in content.splitlines() if l.strip()), "")
            section = page.parent.name.title() if page.parent != wiki_root else "General"
            sections.setdefault(section, []).append(f"- [[{page.stem}]] — {first[:80]}")
        except OSError:
            continue

    lines = [f"---\ntitle: Wiki Index\nupdated: {_today()}\n---\n\n# Wiki Index\n"]
    for section in sorted(sections):
        lines.append(f"\n## {section}\n")
        lines.extend(sorted(sections[section]))
    lines.append("\n")

    if not dry_run:
        (wiki_root / "index.md").write_text("\n".join(lines), encoding="utf-8")
        total = sum(len(v) for v in sections.values())
        _log(f"index: wrote {total} pages")
    else:
        _log(f"index: dry-run, {sum(len(v) for v in sections.values())} pages")

    return sum(len(v) for v in sections.values())


# ── CLI entrypoint ────────────────────────────────────────────────────────────

PHASES = {
    "crosslink": lambda args, paths: phase_crosslink(paths["wiki"], args.dry_run),
    "takes":     lambda args, paths: phase_takes(paths["wiki"], paths["brain"], dry_run=args.dry_run),
    "stale":     lambda args, paths: phase_stale(paths["wiki"], paths["kb"], dry_run=args.dry_run),
    "lessons":   lambda args, paths: phase_lessons(paths["wiki"], paths["kb"], args.dry_run),
    "index":     lambda args, paths: phase_index(paths["wiki"], args.dry_run),
    "all": lambda args, paths: sum([
        phase_lessons(paths["wiki"], paths["kb"], args.dry_run),
        phase_index(paths["wiki"], args.dry_run),
        phase_crosslink(paths["wiki"], args.dry_run),
        phase_takes(paths["wiki"], paths["brain"], dry_run=args.dry_run),
        phase_stale(paths["wiki"], paths["kb"], dry_run=args.dry_run),
    ]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Wiki Synthesizer")
    parser.add_argument("--phase", choices=list(PHASES.keys()), default="all")
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    paths = {
        "wiki":  nova_home / "wiki",
        "kb":    nova_home / "kb",
        "brain": nova_home / "brain.db",
    }

    fn = PHASES[args.phase]
    result = fn(args, paths)
    _log(f"phase={args.phase} result={result}")


if __name__ == "__main__":
    main()
