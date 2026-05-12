"""
examples/kb_quickstart.py — Agent KB Pattern quick start example.

Demonstrates:
  1. Creating a KB with KBManager
  2. Writing config/fix pages
  3. Syncing to SQLite (keyword-only, no API key needed)
  4. Searching with keyword mode

Run:
  python examples/kb_quickstart.py
"""

import os
import sys
import tempfile
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent.parent))

from nova.kb import KBManager, KBSearch, KBSync
from nova.kb.sync import noop_embed


def main():
    # Use a temp dir so the example is self-contained
    with tempfile.TemporaryDirectory(prefix="nova-kb-") as tmp:
        kb_dir = Path(tmp) / "kb"
        db_path = Path(tmp) / "embeddings.db"

        print(f"KB dir: {kb_dir}")
        print(f"DB path: {db_path}\n")

        # ── 1. KBManager: write pages ──────────────────────────────────
        kb = KBManager(kb_dir)

        # Write a config page
        kb.write(
            name="gateway-endpoint",
            subdir="config",
            page_type="config",
            tags=["gateway", "api"],
            status="active",
            title="Gateway API Endpoint",
            body=(
                "## Endpoint\n"
                "https://api.example.com/v1\n\n"
                "## Auth\n"
                "x-api-key header. Key in AGENT_API_KEY env var.\n\n"
                "## Notes\n"
                "Streams NDJSON, not SSE. Use streamGenerateContent endpoint.\n"
            ),
        )

        # Write a fix page
        kb.write(
            name="ssl-cert-error",
            subdir="fixes",
            page_type="fix",
            tags=["ssl", "gateway"],
            status="resolved",
            title="SSL Certificate Error on Gateway",
            body=(
                "## Root Cause\n"
                "Missing REQUESTS_CA_BUNDLE env var in Docker container.\n\n"
                "## Fix\n"
                "Set `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt` in .env\n\n"
                "## Prevention\n"
                "Add cert check to container health check script.\n"
            ),
        )

        # Write a project page
        kb.write(
            name="my-project",
            subdir="projects",
            page_type="project",
            tags=["research"],
            status="active",
            title="My Research Project",
            body=(
                "## Current Phase\n"
                "Data collection — scraping 50 sources.\n\n"
                "## Next Action\n"
                "Run kb sync after each batch, then query for contradictions.\n\n"
                "## Decisions\n"
                "- Use local embedding (sentence-transformers) to avoid API costs\n"
                "- Weekly summary every Monday\n"
            ),
        )

        print("Pages written:")
        for page in kb.pages():
            print(f"  {page.page_type:10} {page.path.relative_to(kb_dir)} — {page.title}")

        # ── 2. KBSync: index into SQLite ───────────────────────────────
        print("\nIndexing KB (keyword mode, no embedding API)...")
        sync = KBSync(kb_dir, db_path, embed_fn=noop_embed)
        stats = sync.run(verbose=True)
        print(f"Sync stats: {stats}")
        print(f"DB stats:   {sync.stats()}")

        # ── 3. KBSearch: keyword search ────────────────────────────────
        print("\nSearching for 'ssl certificate'...")
        search = KBSearch(kb_dir, db_path)
        results = search.query("ssl certificate", mode="keyword", top_k=3)
        for r in results:
            print(f"  [{r['score']:.3f}] {r['path']}  →  {r['snippet'][:80]}")

        print("\nSearching for 'api endpoint'...")
        results = search.query("api endpoint", mode="keyword", top_k=3)
        for r in results:
            print(f"  [{r['score']:.3f}] {r['path']}  →  {r['snippet'][:80]}")

        # ── 4. MEMORY.md two-layer demo ────────────────────────────────
        print("\n── MEMORY.md (hot cache) ──")
        memory_path = Path(tmp) / "MEMORY.md"
        memory_path.write_text(
            "Gateway endpoint: api.example.com/v1. KB: [[config/gateway-endpoint]]\n"
            "[ACTIVE] my-project — data collection. Next: sync after each batch. "
            "KB: [[projects/my-project]]\n"
        )
        print(memory_path.read_text())

        print("✓ Example complete. See nova/kb/ for full implementation.")


if __name__ == "__main__":
    main()
