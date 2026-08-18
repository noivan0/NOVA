"""tests/unit/test_syscall_spawn_validation.py — regression test for
KernelAPI.spawn()'s missing harness-name validation.

SECURITY-011 (2026-08-18, deep audit round 4): `harness` was accepted
with zero validation and stored verbatim in `nova_events.title` as
`f"spawn:{harness}"`. brain_watcher's spawn-polling loop
(nova/watcher/brain.py) later extracts this title back out with a naive
`split(":", 1)[1]` and passes it straight to
`HarnessLoader.load(harness_name)`. Reproduced:
`spawn(harness="../../../etc/passwd", ...)` was accepted and persisted
verbatim with no error. HarnessLoader.load() itself now rejects
traversal (SECURITY-008), but spawn() is a separate trust boundary --
any code with brain.db write access could otherwise queue an
arbitrary/malformed harness name. Fixed by rejecting anything that
isn't a plain filesystem-safe segment ([A-Za-z0-9_-]{1,128}) before it's
ever persisted.

SECURITY-013 (2026-08-18, self-caught during verification of the above
fix): the initial fix used `_HARNESS_NAME_RE.match()`, which lets
Python's `re` engine treat `$` as matching just before a trailing
newline (not strictly end-of-string) -- "research\n" incorrectly passed
the check. brain.py's downstream `.strip()` happens to neutralize this
specific payload today, but the validation itself needed to be airtight
independently of that. Fixed by switching to `fullmatch()`, which
anchors both ends unconditionally.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from nova.kernel.syscall import KernelAPI, NovaSyscallError

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
        CREATE TABLE nova_events (
            id TEXT PRIMARY KEY, event_type TEXT, severity TEXT,
            title TEXT, detail TEXT, source_agent TEXT, is_read INTEGER,
            created_at TEXT
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


def test_spawn_rejects_path_traversal_harness_name(api: KernelAPI):
    with pytest.raises(NovaSyscallError):
        api.spawn(harness="../../../etc/passwd", task="x", agent="harness")


@pytest.mark.parametrize("evil_harness", [
    "../../../etc/passwd",
    "../outside",
    "a/../b",
    "/etc/passwd",
    "research; rm -rf ~",
    "research\nmalicious",
    "",
    "a" * 200,
    "research\n",
    "valid\n../../../etc/passwd",
])
def test_spawn_rejects_various_malformed_harness_names(api: KernelAPI, evil_harness: str):
    with pytest.raises(NovaSyscallError):
        api.spawn(harness=evil_harness, task="x", agent="harness")


def test_spawn_accepts_normal_harness_names(api: KernelAPI):
    for name in ("research", "code_implement", "nuuseta_research", "verification_gate"):
        handle = api.spawn(harness=name, task="normal task", agent="harness")
        assert handle.run_id
