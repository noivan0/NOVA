"""
examples/engines/synthesize.py — Minimal synthesize engine for BrainWatcher.

Triggered when brain.db accumulates +15 new takes.

This example synthesizes recent takes into a structured KB summary page.
Replace with your own synthesis logic (e.g. call an LLM, run clustering, etc.).

Usage:
    python examples/engines/synthesize.py --nova-home ~/.nova
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA Synthesize Engine — minimal example")
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory",
    )
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    brain_db = nova_home / "brain.db"
    kb_dir = nova_home / "kb"

    if not brain_db.exists():
        print("[synthesize] brain.db not found — nothing to do")
        return

    try:
        db = sqlite3.connect(str(brain_db), timeout=5)
        rows = db.execute(
            "SELECT claim, weight FROM takes ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"[synthesize] db error: {e}")
        raise SystemExit(1)

    if not rows:
        print("[synthesize] no takes found")
        return

    # Write a simple KB summary page
    kb_dir.mkdir(parents=True, exist_ok=True)
    out = kb_dir / "synthesis" / f"synthesis-{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"---\ntitle: Synthesis {date.today().isoformat()}\ntype: synthesis\n---\n",
        f"# Synthesis — {date.today().isoformat()}\n",
        "\n## Recent Insights\n",
    ]
    for claim, weight in rows:
        lines.append(f"- {claim[:200]} *(weight={weight:.2f})*")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[synthesize] wrote {len(rows)} takes → {out}")


if __name__ == "__main__":
    main()
