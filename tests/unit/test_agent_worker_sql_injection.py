"""tests/unit/test_agent_worker_sql_injection.py — regression tests for
SQL injection / DoS in nova_agent_worker.py's KB fallback search and
nova_shared_kb.py's keyword search.

SECURITY-015/016 (2026-08-18, deep audit round 5): `_read_context_fallback()`
(nova/agents/scripts/nova_agent_worker.py) and `read_context()`
(nova/agents/scripts/nova_shared_kb.py) built LIKE conditions via raw
f-string interpolation of user-controlled keywords derived from `topic`
-- which originates from CLI `--context topic=...`, the same
attacker-controlled input class that caused SECURITY-003 (shell
injection). Reproduced: an entirely ordinary English word containing an
apostrophe (e.g. "don't") crashed the SQL query with a syntax error --
an unauthenticated DoS via a single quote in normal user text. Fixed by
using parameterized placeholders (`?`) instead of string-formatting
keyword values into the SQL text.
"""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module(rel_path: str, name: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_page_chunks_db(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE page_chunks (section TEXT, content TEXT)")
    conn.executemany("INSERT INTO page_chunks VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def worker_mod(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.db"
    _make_page_chunks_db(db_path, [("normal", "hello world testing")])
    # nova_agent_worker.py runs `_inject_api_key()` at IMPORT TIME (module
    # top level), which can os.environ.setdefault() NOVA_LLM_PROVIDER and
    # friends -- monkeypatch can't auto-revert writes it didn't make, so
    # loading this module via importlib leaks those vars into every
    # subsequent test in the same pytest process unless we snapshot/restore
    # explicitly here (discovered via git-stash bisection while chasing an
    # unrelated tests/unit/test_config.py failure caused by exactly this).
    _env_keys = (
        "NOVA_LLM_PROVIDER", "NOVA_LLM_MODEL", "NOVA_LLM_API_KEY",
        "HMG_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "NOVA_KB_EMBEDDING_API_KEY", "NOVA_CODEX_API_KEY",
        "NOVA_IMAGE_GEN_API_KEY",
    )
    _before = {k: os.environ.get(k) for k in _env_keys}
    mod = _load_module(
        "nova/agents/scripts/nova_agent_worker.py", "nova_agent_worker_test_mod"
    )
    monkeypatch.setattr(mod, "BRAIN_DB", db_path)
    yield mod
    for k, v in _before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.parametrize("payload", [
    "don't",
    "it's a test",
    "xxx%'OR1=1--",
    "what's up",
    "can't won't shouldn't",
])
def test_read_context_fallback_does_not_crash_on_apostrophes(worker_mod, payload):
    """Regression test for the exact class of payload that crashed the
    query before the fix (any ordinary English contraction)."""
    result = worker_mod._read_context_fallback(payload)
    assert isinstance(result, str)  # must not raise


def test_read_context_fallback_still_finds_normal_matches(worker_mod):
    result = worker_mod._read_context_fallback("testing something")
    assert "hello world testing" in result


@pytest.fixture
def shared_kb_mod(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE page_chunks (section TEXT, content TEXT);
        CREATE TABLE pages (id TEXT, path TEXT, title TEXT);
        CREATE VIRTUAL TABLE pages_fts USING fts5(compiled_truth);
        """
    )
    conn.execute("INSERT INTO page_chunks VALUES (?, ?)", ("normal", "hello world testing"))
    conn.commit()
    conn.close()
    mod = _load_module("nova/agents/scripts/nova_shared_kb.py", "nova_shared_kb_test_mod")
    monkeypatch.setattr(mod, "BRAIN_DB", db_path)
    return mod


@pytest.mark.parametrize("payload", [
    "don't",
    "it's a test",
    "xxx%'OR1=1--",
])
def test_read_context_does_not_crash_on_apostrophes(shared_kb_mod, payload):
    result = shared_kb_mod.read_context(payload, agent="test")
    assert isinstance(result, str)  # must not raise
