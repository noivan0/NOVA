"""
nova.kb.sync — Incremental embedding sync for KB pages.

Indexes KB markdown files into SQLite (embeddings.db).
Only re-embeds pages whose content has changed (content_hash check).

Implements the Agent KB Pattern:
  https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18

Supports pluggable embedding backends:
  - OpenAI / any OpenAI-compatible endpoint
  - sentence-transformers (local)
  - Ollama (local)
  - No-op (keyword-only search fallback)

Usage::

    from nova.kb.sync import KBSync

    # OpenAI
    sync = KBSync("~/.agent/kb", db_path="~/.agent/embeddings.db",
                  embed_fn=openai_embed)

    # Local (sentence-transformers)
    sync = KBSync("~/.agent/kb", db_path="~/.agent/embeddings.db",
                  embed_fn=local_embed)

    stats = sync.run()  # returns {"indexed": N, "skipped": M, "errors": K}

CLI::

    python -m nova.kb.sync --kb ~/.agent/kb --db ~/.agent/embeddings.db
    python -m nova.kb.sync --kb ~/.agent/kb --db ~/.agent/embeddings.db --reindex-all
    python -m nova.kb.sync --kb ~/.agent/kb --db ~/.agent/embeddings.db --stats
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Files excluded from indexing (navigation / schema files)
EXCLUDE_FILES = frozenset({"index.md", "log.md", "log-2026.md", "SCHEMA.md", "TEMPLATE.md"})
EXCLUDE_DIRS = frozenset({"archive", "archived", "_archive"})

EmbedFn = Callable[[str], Optional[list[float]]]


# ------------------------------------------------------------------ #
# Built-in embedding backend helpers                                  #
# ------------------------------------------------------------------ #

def openai_embed(text: str, *, api_key: str, base_url: str = "https://api.openai.com/v1",
                 model: str = "text-embedding-3-large") -> Optional[list[float]]:
    """OpenAI / OpenAI-compatible embedding endpoint."""
    try:
        import requests
        r = requests.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": text[:8000], "model": model},
            timeout=15,
            verify=True,
        )
        if r.status_code == 200:
            return r.json()["data"][0]["embedding"]
        return None
    except Exception:
        return None


def local_embed(text: str, *, model_name: str = "all-MiniLM-L6-v2") -> Optional[list[float]]:
    """sentence-transformers local embedding (no API calls)."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        m = SentenceTransformer(model_name)
        return m.encode(text).tolist()
    except ImportError:
        raise RuntimeError(
            "sentence-transformers not installed. Run: pip install sentence-transformers"
        )


def ollama_embed(text: str, *, base_url: str = "http://localhost:11434",
                 model: str = "nomic-embed-text") -> Optional[list[float]]:
    """Ollama local embedding."""
    try:
        import requests
        r = requests.post(
            f"{base_url.rstrip('/')}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=15,
        )
        if r.ok:
            return r.json().get("embedding")
        return None
    except Exception:
        return None


def noop_embed(text: str) -> None:  # type: ignore[return]
    """Disable embedding — keyword-only search fallback."""
    return None


# ------------------------------------------------------------------ #
# Chunking                                                            #
# ------------------------------------------------------------------ #

def chunk_by_h2(content: str, max_chars: int = 2000) -> list[tuple[str, str]]:
    """
    Split a markdown page into (title, text) chunks at H2 boundaries.
    Falls back to a single chunk for pages without H2 sections.
    """
    # Strip YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            content = content[end + 3:].strip()

    sections = re.split(r"^(## .+)$", content, flags=re.MULTILINE)
    chunks: list[tuple[str, str]] = []
    current_title = "intro"
    current_text = ""

    for part in sections:
        if part.startswith("## "):
            if current_text.strip():
                chunks.append((current_title, current_text.strip()))
            current_title = part.strip("# ").strip()
            current_text = part + "\n"
        else:
            current_text += part

    if current_text.strip():
        chunks.append((current_title, current_text.strip()))

    # Split oversized chunks (no H2 inside)
    result = []
    for title, text in chunks:
        if len(text) <= max_chars:
            result.append((title, text))
        else:
            for i in range(0, len(text), max_chars):
                result.append((f"{title} (part {i // max_chars + 1})", text[i:i + max_chars]))

    return result or [("full", content)]


# ------------------------------------------------------------------ #
# SQLite helpers                                                      #
# ------------------------------------------------------------------ #

def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_embeddings (
            id           TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            title        TEXT,
            chunk_idx    INTEGER DEFAULT 0,
            content_hash TEXT,
            embedding    BLOB,
            indexed_at   TEXT,
            char_count   INTEGER
        )
    """)
    conn.commit()


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ------------------------------------------------------------------ #
# KBSync                                                              #
# ------------------------------------------------------------------ #

class KBSync:
    """
    Incremental KB embedding sync.

    Only re-embeds pages whose content_hash has changed since last sync.
    Stores vectors as raw float32 blobs in SQLite — no external vector DB needed.
    """

    def __init__(
        self,
        kb_root: str | Path,
        db_path: str | Path,
        embed_fn: EmbedFn = noop_embed,
        namespace: str = "kb_embeddings",
    ):
        self.kb_root = Path(kb_root).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve()
        self.embed_fn = embed_fn
        self.namespace = namespace

    def run(
        self,
        reindex_all: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict[str, int]:
        """
        Sync all KB pages to the embedding DB.

        Returns:
            {"indexed": N, "skipped": M, "errors": K}
        """
        stats = {"indexed": 0, "skipped": 0, "errors": 0}

        with sqlite3.connect(self.db_path) as conn:
            _init_db(conn)

            for md_path in self._scan():
                relative = str(md_path.relative_to(self.kb_root))
                content = md_path.read_text(encoding="utf-8", errors="replace")
                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

                if not reindex_all:
                    row = conn.execute(
                        f"SELECT content_hash FROM {self.namespace} WHERE path = ? AND chunk_idx = 0",
                        (relative,),
                    ).fetchone()
                    if row and row[0] == content_hash:
                        stats["skipped"] += 1
                        continue

                chunks = chunk_by_h2(content)
                now = datetime.utcnow().isoformat()

                if not dry_run:
                    # Delete old chunks for this page
                    conn.execute(f"DELETE FROM {self.namespace} WHERE path = ?", (relative,))

                for i, (chunk_title, chunk_text) in enumerate(chunks):
                    chunk_id = f"{relative}::{i}"
                    vec = self.embed_fn(chunk_text) if not dry_run else None
                    blob = _vec_to_blob(vec) if vec else None

                    if verbose:
                        print(f"  {'DRY' if dry_run else 'IDX'} {relative} chunk={i} "
                              f"title={chunk_title!r:.40} chars={len(chunk_text)}")

                    if not dry_run:
                        conn.execute(
                            f"""INSERT OR REPLACE INTO {self.namespace}
                                (id, path, title, chunk_idx, content_hash, embedding, indexed_at, char_count)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (chunk_id, relative, chunk_title, i, content_hash, blob, now, len(chunk_text)),
                        )

                if not dry_run:
                    conn.commit()
                stats["indexed"] += 1

        return stats

    def stats(self) -> dict[str, Any]:
        """Return current DB statistics."""
        with sqlite3.connect(self.db_path) as conn:
            _init_db(conn)
            total = conn.execute(f"SELECT COUNT(*) FROM {self.namespace}").fetchone()[0]
            with_vec = conn.execute(
                f"SELECT COUNT(*) FROM {self.namespace} WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            pages = conn.execute(
                f"SELECT COUNT(DISTINCT path) FROM {self.namespace}"
            ).fetchone()[0]
        return {"total_chunks": total, "chunks_with_vector": with_vec, "unique_pages": pages}

    def _scan(self):
        for md in self.kb_root.rglob("*.md"):
            if md.name in EXCLUDE_FILES:
                continue
            if any(part in EXCLUDE_DIRS for part in md.parts):
                continue
            yield md


# ------------------------------------------------------------------ #
# CLI entrypoint                                                      #
# ------------------------------------------------------------------ #

def _main() -> None:
    import argparse, os

    parser = argparse.ArgumentParser(description="nova.kb.sync — KB embedding sync")
    parser.add_argument("--kb", default=os.environ.get("AGENT_KB_PATH", "~/.agent/kb"),
                        help="KB root directory")
    parser.add_argument("--db", default=os.environ.get("AGENT_DB_PATH", "~/.agent/embeddings.db"),
                        help="SQLite DB path")
    parser.add_argument("--reindex-all", action="store_true", help="Force re-embed all pages")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be indexed")
    parser.add_argument("--stats", action="store_true", help="Show DB stats and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--backend", choices=["openai", "local", "ollama", "none"],
                        default="none", help="Embedding backend")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""),
                        help="API key for openai backend")
    parser.add_argument("--base-url", default="https://api.openai.com/v1",
                        help="Base URL for openai backend")
    args = parser.parse_args()

    if args.backend == "openai":
        embed_fn: EmbedFn = lambda t: openai_embed(t, api_key=args.api_key, base_url=args.base_url)
    elif args.backend == "local":
        embed_fn = local_embed
    elif args.backend == "ollama":
        embed_fn = ollama_embed
    else:
        embed_fn = noop_embed

    sync = KBSync(args.kb, args.db, embed_fn=embed_fn)

    if args.stats:
        s = sync.stats()
        print(f"Chunks: {s['total_chunks']} total, {s['chunks_with_vector']} with vector, "
              f"{s['unique_pages']} unique pages")
        return

    stats = sync.run(reindex_all=args.reindex_all, dry_run=args.dry_run, verbose=args.verbose)
    print(f"Indexed: {stats['indexed']}  Skipped: {stats['skipped']}  Errors: {stats['errors']}")


if __name__ == "__main__":
    _main()
