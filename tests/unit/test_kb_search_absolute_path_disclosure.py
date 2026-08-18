"""tests/unit/test_kb_search_absolute_path_disclosure.py — regression test
for KBSearch's potential arbitrary file read via an absolute/escaping
path stored in the embeddings DB.

SECURITY-009 (2026-08-18, Codex-audited round 3): `_search_namespace()`
used `Path(kb_root) / path if not Path(path).is_absolute() else
Path(path)` — if a row's `path` column were ever absolute (or escaped
kb_root via ../), the file was read verbatim and its content disclosed
in search results/snippets. nova/kb/sync.py, the only normal writer,
always stores kb-root-relative paths (via
`md_path.relative_to(self.kb_root)`), so this required an abnormal DB
row to exploit — but defense-in-depth shouldn't rely on that always
holding. Codex reproduced disclosure of a file's content via a crafted
DB row with an absolute path. Fixed by resolving the path under kb_root
and skipping (not disclosing) any row whose resolved path escapes it.
"""
import sqlite3
import struct
from pathlib import Path

import pytest

from nova.kb.search import KBSearch


def _make_embeddings_db(db_path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE kb_embeddings "
        "(id TEXT, path TEXT, title TEXT, chunk_idx INTEGER, "
        "embedding BLOB, char_count INTEGER)"
    )
    con.executemany(
        "INSERT INTO kb_embeddings VALUES (?, ?, ?, ?, ?, ?)", rows
    )
    con.commit()
    con.close()


def test_absolute_path_row_is_not_disclosed(tmp_path: Path):
    root = tmp_path / "kb"
    root.mkdir()
    outside = tmp_path / "outside-secret.md"
    outside.write_text("ULTRA_SECRET_AUDIT_MARKER alpha-search-token")

    db = tmp_path / "embeddings.db"
    _make_embeddings_db(db, [
        ("p1", str(outside), "outside", 0, struct.pack("f", 0.0), 1),
    ])

    results = KBSearch(root, db).query("alpha-search-token", mode="keyword")
    assert not any(
        "ULTRA_SECRET_AUDIT_MARKER" in r.get("snippet", "") for r in results
    ), "absolute-path row content was disclosed in search results"


def test_traversal_relative_path_row_is_not_disclosed(tmp_path: Path):
    root = tmp_path / "kb"
    root.mkdir()
    outside = tmp_path / "outside-secret2.md"
    outside.write_text("SECOND_SECRET_MARKER beta-search-token")

    db = tmp_path / "embeddings.db"
    _make_embeddings_db(db, [
        ("p2", "../outside-secret2.md", "outside2", 0, struct.pack("f", 0.0), 1),
    ])

    results = KBSearch(root, db).query("beta-search-token", mode="keyword")
    assert not any(
        "SECOND_SECRET_MARKER" in r.get("snippet", "") for r in results
    )


def test_normal_relative_path_row_still_works(tmp_path: Path):
    root = tmp_path / "kb"
    root.mkdir()
    page = root / "normal-page.md"
    page.write_text("NORMAL_CONTENT gamma-search-token")

    db = tmp_path / "embeddings.db"
    _make_embeddings_db(db, [
        ("p3", "normal-page.md", "normal", 0, struct.pack("f", 0.0), 1),
    ])

    results = KBSearch(root, db).query("gamma-search-token", mode="keyword")
    assert any("NORMAL_CONTENT" in r.get("snippet", "") for r in results)
