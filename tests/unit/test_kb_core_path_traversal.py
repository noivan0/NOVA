"""tests/unit/test_kb_core_path_traversal.py — regression tests for
nova/core/kb.py's KB._resolve() path traversal vulnerability.

SECURITY-004 (2026-08-18, deep audit): the original sanitizer
(`key.lstrip("/").replace("..", "")`) was a single-pass string replace,
not a real path-traversal defense. A key like
'../../../../../../tmp/evil' collapsed into a path that Path.__truediv__
treated as absolute, silently discarding self.root entirely. Confirmed
with a real filesystem write landing outside kb.root (and reproduced via
the `nova kb write` CLI command, which passes a user-supplied key straight
through to KB.write()).
"""
import tempfile
from pathlib import Path

import pytest

from nova.core.kb import KB


@pytest.fixture
def kb(tmp_path: Path) -> KB:
    return KB(path=str(tmp_path / "kb_root"))


def test_normal_key_resolves_under_root(kb: KB):
    p = kb._resolve("projects/my-harness")
    assert str(p).startswith(str(kb.root))
    assert p.suffix == ".md"


@pytest.mark.parametrize("evil_key", [
    "../../../../../../tmp/nova_kb_poc",
    "../../etc/passwd",
    "a/../../../../etc/passwd",
    "....//....//....//etc/passwd",
    "/../../etc/passwd",
])
def test_traversal_keys_are_rejected(kb: KB, evil_key: str):
    with pytest.raises(ValueError):
        kb._resolve(evil_key)


def test_write_with_traversal_key_never_touches_filesystem_outside_root(kb: KB, tmp_path: Path):
    outside_target = tmp_path / "outside_marker.md"
    assert not outside_target.exists()
    evil_key = f"../{outside_target.name}".replace(".md", "")
    with pytest.raises(ValueError):
        kb.write(evil_key, "should never be written")
    assert not outside_target.exists()


def test_read_with_traversal_key_is_rejected(kb: KB):
    with pytest.raises(ValueError):
        kb.read("../../../../etc/passwd")


def test_delete_with_traversal_key_is_rejected(kb: KB):
    with pytest.raises(ValueError):
        kb.delete("../../../../etc/passwd")
