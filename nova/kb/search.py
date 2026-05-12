"""
nova.kb.search — Hybrid BM25 + cosine search across KB namespaces.

No external vector DB required — runs entirely on SQLite.
Supports multi-namespace search (KB + sessions + skills + project-specific KBs).

Implements the Agent KB Pattern:
  https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18

Usage::

    from nova.kb.search import KBSearch

    search = KBSearch("~/.agent/kb", db_path="~/.agent/embeddings.db")

    # Hybrid (keyword + vector)
    results = search.query("SSL certificate error", top_k=5)

    # Keyword only (no API call)
    results = search.query("SSL error", mode="keyword")

    # Semantic only
    results = search.query("SSL error", mode="semantic", embed_fn=my_embed)

    for r in results:
        print(r["score"], r["path"], r["title"])
        print(r["snippet"])
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any, Callable, Optional

EmbedFn = Callable[[str], Optional[list[float]]]

EXCLUDE_FILES = frozenset({"index.md", "log.md", "SCHEMA.md", "TEMPLATE.md"})


# ------------------------------------------------------------------ #
# BM25 keyword search (no index required, grep-based)                #
# ------------------------------------------------------------------ #

def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b[a-zA-Z가-힣][a-zA-Z가-힣0-9_-]{1,}\b", text.lower())


def _bm25_score(query_tokens: list[str], doc_tokens: list[str],
                k1: float = 1.5, b: float = 0.75, avg_len: float = 300.0) -> float:
    tf_map: dict[str, int] = {}
    for t in doc_tokens:
        tf_map[t] = tf_map.get(t, 0) + 1

    score = 0.0
    dl = len(doc_tokens)
    for token in set(query_tokens):
        tf = tf_map.get(token, 0)
        if tf == 0:
            continue
        idf = math.log(1 + 1)  # simplified IDF (no corpus stats)
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * dl / avg_len)
        score += idf * numerator / denominator
    return score


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ------------------------------------------------------------------ #
# KBSearch                                                            #
# ------------------------------------------------------------------ #

class KBSearch:
    """
    Hybrid KB search: BM25 keyword + cosine vector similarity.

    Supports multiple namespaces (e.g., main KB + project KB + sessions).
    Each namespace is a table in the same SQLite DB (or a separate DB).
    """

    def __init__(
        self,
        kb_root: str | Path,
        db_path: str | Path,
        namespaces: Optional[list[dict[str, Any]]] = None,
    ):
        self.kb_root = Path(kb_root).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve()

        # Default: single namespace pointing at the main KB
        self.namespaces = namespaces or [
            {
                "table": "kb_embeddings",
                "label": "KB",
                "kb_root": self.kb_root,
                "db_path": self.db_path,
            }
        ]

    def query(
        self,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",  # "hybrid" | "keyword" | "semantic"
        embed_fn: Optional[EmbedFn] = None,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """
        Search KB pages.

        Args:
            query:     Natural language query string
            top_k:     Max results to return
            mode:      "hybrid" (default), "keyword", or "semantic"
            embed_fn:  Required for "semantic" and "hybrid" modes
            min_score: Filter results below this score threshold

        Returns:
            List of dicts: {path, title, score, snippet, source_label, chunk_idx}
        """
        query_tokens = _tokenize(query)
        query_vec: Optional[list[float]] = None
        if mode in ("hybrid", "semantic") and embed_fn:
            query_vec = embed_fn(query)

        all_results: list[dict[str, Any]] = []

        for ns in self.namespaces:
            ns_results = self._search_namespace(
                query=query,
                query_tokens=query_tokens,
                query_vec=query_vec,
                mode=mode,
                top_k=top_k * 2,  # over-fetch, dedupe later
                ns=ns,
            )
            all_results.extend(ns_results)

        # Deduplicate by (path, chunk_idx), keep highest score
        seen: dict[str, dict] = {}
        for r in all_results:
            key = f"{r['path']}::{r['chunk_idx']}"
            if key not in seen or r["score"] > seen[key]["score"]:
                seen[key] = r

        ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
        filtered = [r for r in ranked if r["score"] >= min_score]
        return filtered[:top_k]

    def _search_namespace(
        self,
        query: str,
        query_tokens: list[str],
        query_vec: Optional[list[float]],
        mode: str,
        top_k: int,
        ns: dict[str, Any],
    ) -> list[dict[str, Any]]:
        table = ns["table"]
        db_path = ns.get("db_path", self.db_path)
        kb_root = ns.get("kb_root", self.kb_root)
        label = ns.get("label", table)

        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            return []

        try:
            rows = conn.execute(
                f"SELECT id, path, title, chunk_idx, embedding, char_count FROM {table}"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

        results = []
        for row_id, path, title, chunk_idx, emb_blob, char_count in rows:
            # Read the actual file content for keyword scoring + snippet
            full_path = Path(kb_root) / path if not Path(path).is_absolute() else Path(path)
            try:
                content = full_path.read_text(errors="replace")
            except OSError:
                content = title or ""

            doc_tokens = _tokenize(content)
            kw_score = _bm25_score(query_tokens, doc_tokens) if mode in ("hybrid", "keyword") else 0.0
            sem_score = 0.0

            if mode in ("hybrid", "semantic") and query_vec and emb_blob:
                doc_vec = _blob_to_vec(emb_blob)
                sem_score = _cosine(query_vec, doc_vec)

            if mode == "hybrid":
                score = 0.4 * min(kw_score / 5.0, 1.0) + 0.6 * sem_score
            elif mode == "keyword":
                score = kw_score
            else:  # semantic
                score = sem_score

            if score <= 0:
                continue

            snippet = self._extract_snippet(content, query_tokens)

            results.append({
                "path": path,
                "title": title or Path(path).stem,
                "score": round(score, 4),
                "snippet": snippet,
                "source_label": label,
                "chunk_idx": chunk_idx or 0,
            })

        return sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]

    def _extract_snippet(self, content: str, query_tokens: list[str], max_len: int = 200) -> str:
        """Extract the most relevant snippet from content."""
        lines = content.splitlines()
        best_line = ""
        best_score = -1

        for line in lines:
            line_tokens = _tokenize(line)
            hits = sum(1 for t in query_tokens if t in line_tokens)
            if hits > best_score and len(line) > 20:
                best_score = hits
                best_line = line

        return best_line.strip()[:max_len] if best_line else content[:max_len]


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #

def _main() -> None:
    import argparse, os

    parser = argparse.ArgumentParser(description="nova.kb.search — KB hybrid search")
    parser.add_argument("query", nargs="?", default="", help="Search query")
    parser.add_argument("--kb", default=os.environ.get("AGENT_KB_PATH", "~/.agent/kb"))
    parser.add_argument("--db", default=os.environ.get("AGENT_DB_PATH", "~/.agent/embeddings.db"))
    parser.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="keyword")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if not args.query:
        parser.print_help()
        return

    search = KBSearch(args.kb, args.db)
    results = search.query(args.query, top_k=args.top_k, mode=args.mode)

    if not results:
        print("No results found.")
        return

    for i, r in enumerate(results, 1):
        print(f"\n[{i}] score={r['score']}  {r['path']}  ({r['source_label']})")
        print(f"    Title: {r['title']}")
        print(f"    {r['snippet']}")


if __name__ == "__main__":
    _main()
