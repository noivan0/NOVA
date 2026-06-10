"""
nova.engine.learn — Learn Engine: link new takes to KB pages.

Called by BrainWatcher when brain.db accumulates +5 new takes.
Lightweight, fast (30-min cooldown).

What it does:
  1. Find takes that have no page_id (orphan takes)
  2. Score each take against KB pages using word overlap
  3. Assign the best-matching page_id
  4. Record a system event on completion
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import time
from pathlib import Path


# ── tokenisation (no external deps) ──────────────────────────────────────────

_STOP = frozenset(
    "the a an and or not is was are were be been being have has had do does did "
    "will would could should may might shall can to of in on at for with by from "
    "this that these those it its i you he she we they what which who how when where".split()
)


def _tok(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP]


def _score(claim_toks: list[str], page_text: str) -> float:
    if not claim_toks:
        return 0.0
    page_toks = set(_tok(page_text))
    hits = sum(1 for t in claim_toks if t in page_toks)
    return hits / math.sqrt(len(claim_toks) * max(len(page_toks), 1))


# ── main ──────────────────────────────────────────────────────────────────────

def run(nova_home: Path) -> int:
    """Link orphan takes to KB pages. Returns count of linked takes."""
    brain_db = nova_home / "brain.db"
    kb_root = nova_home / "kb"

    if not brain_db.exists():
        print("[learn] brain.db not found — skip")
        return 0

    # Load KB pages (path → text snippet for scoring)
    kb_pages: list[tuple[str, str, str]] = []  # (page_id, path, text)
    if kb_root.exists():
        for md in kb_root.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")[:2000]
                kb_pages.append((str(md.relative_to(kb_root)), str(md.relative_to(kb_root)), text))
            except OSError:
                pass

    db = sqlite3.connect(str(brain_db), timeout=10)
    db.row_factory = sqlite3.Row

    # Get all KB page ids for cross-reference
    page_rows = db.execute("SELECT id, path, compiled_truth FROM pages LIMIT 2000").fetchall()
    page_map: dict[str, tuple[str, str]] = {r["id"]: (r["path"], r["compiled_truth"] or "") for r in page_rows}

    # Find orphan takes (no page_id)
    orphans = db.execute(
        "SELECT id, claim FROM takes WHERE page_id IS NULL ORDER BY created_at DESC LIMIT 100"
    ).fetchall()

    linked = 0
    for take in orphans:
        claim_toks = _tok(take["claim"])
        best_id, best_score = None, 0.0

        for pid, (path, truth) in page_map.items():
            s = _score(claim_toks, truth or path)
            if s > best_score:
                best_score, best_id = s, pid

        if best_id and best_score > 0.05:
            db.execute("UPDATE takes SET page_id=? WHERE id=?", (best_id, take["id"]))
            linked += 1

    db.commit()

    total = db.execute("SELECT count(*) FROM takes").fetchone()[0]
    print(f"[learn] takes={total} orphans={len(orphans)} linked={linked}")
    db.close()
    return linked


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Learn Engine")
    parser.add_argument("--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"))
    args = parser.parse_args()
    nova_home = Path(args.nova_home).expanduser().resolve()
    nova_home.mkdir(parents=True, exist_ok=True)
    run(nova_home)


if __name__ == "__main__":
    main()
