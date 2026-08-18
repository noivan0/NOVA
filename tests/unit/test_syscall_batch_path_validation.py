"""tests/unit/test_syscall_batch_path_validation.py — regression test for
KernelAPI.kb_write_batch()'s missing path validation.

SECURITY-005 (2026-08-18, deep audit): kb_write_batch() never called
_validate_path(), unlike kb_write()/kb_delete(). can_write() alone is
insufficient because ownership.yaml glob patterns (e.g. "workspace/**")
are matched against the RAW unnormalized string — a path like
"workspace/../../../etc/cron.d/evil" still starts with "workspace/" and
matches the glob, then got persisted to the DB verbatim with the
traversal intact. Reproduced: an agent with only "workspace/**" write
access (the most common grant in ownership.yaml, held by the built-in
"harness" agent) could smuggle an unnormalized ../-escaping path through
this method alone, even though kb_write() correctly rejects the exact
same input.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from nova.kernel.syscall import KernelAPI, NovaSyscallError, NovaPermissionError

_OWNERSHIP_YAML = str(
    Path(__file__).resolve().parent.parent.parent / "nova" / "kernel" / "ownership.yaml"
)


def _make_brain_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pages (
            id TEXT PRIMARY KEY, path TEXT, title TEXT, page_type TEXT,
            agent TEXT, compiled_truth TEXT, content_hash TEXT,
            char_count INTEGER, indexed_at TEXT, updated_at TEXT
        );
        CREATE TABLE takes (
            id TEXT PRIMARY KEY, kind TEXT, holder TEXT, claim TEXT,
            created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def api(tmp_path: Path) -> KernelAPI:
    db_path = str(tmp_path / "brain.db")
    _make_brain_db(db_path)
    return KernelAPI(brain_db=db_path, ownership_yaml=_OWNERSHIP_YAML)


def test_kb_write_batch_rejects_traversal_path_that_glob_matches(api: KernelAPI):
    """The 'harness' agent legitimately owns workspace/** in ownership.yaml.
    A path traversal payload prefixed with workspace/ still matches that
    glob against the raw string, so this must be caught by explicit path
    normalization/validation, not by the ownership glob alone."""
    evil_path = "workspace/../../../../../etc/cron.d/evil"
    with pytest.raises(NovaSyscallError):
        api.kb_write_batch([{"path": evil_path, "content": "pwned", "agent": "harness"}])


def test_kb_write_batch_and_kb_write_enforce_identical_policy(api: KernelAPI):
    """kb_write() and kb_write_batch() must reject the exact same input --
    this is the regression check that would have caught the original API
    inconsistency (kb_write raised, kb_write_batch silently succeeded)."""
    evil_path = "workspace/../../../../../etc/cron.d/evil"

    with pytest.raises(NovaSyscallError) as single_exc:
        api.kb_write(path=evil_path, content="pwned", agent="harness")

    with pytest.raises(NovaSyscallError) as batch_exc:
        api.kb_write_batch([{"path": evil_path, "content": "pwned", "agent": "harness"}])

    # Both must fail with the same class of error (path validation),
    # not one succeeding and the other rejecting.
    assert type(single_exc.value) is type(batch_exc.value)


def test_kb_write_batch_normal_path_still_works(api: KernelAPI):
    ids = api.kb_write_batch([
        {"path": "workspace/normal/file.md", "content": "ok", "agent": "harness"},
    ])
    assert len(ids) == 1
    assert ids[0]
