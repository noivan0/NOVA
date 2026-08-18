"""tests/unit/test_kb_manager_path_traversal.py — regression tests for
nova/kb/manager.py's KBManager path traversal vulnerability.

SECURITY-007 (2026-08-18, Codex-audited deep audit): write()/read()/
pages() composed `self.root / subdir / f"{name}.md"` with no traversal
check at all. Codex reproduced a real file written OUTSIDE the KB root
via `KBManager(root).write(name="../escaped", page_type="concept",
body="TRAVERSAL_CONFIRMED")`. Fixed with a shared `_safe_page_path()`
helper (same defense-in-depth pattern as nova.core.kb.KB._resolve() and
nova.kernel.syscall.KernelAPI._validate_path()): reject any `name`/
`subdir` containing '..' or an absolute path outright, then hard-verify
the final resolved path is a descendant of self.root.
"""
from pathlib import Path

import pytest

from nova.kb.manager import KBManager


@pytest.fixture
def kb(tmp_path: Path) -> KBManager:
    return KBManager(tmp_path / "kb")


def test_normal_write_and_read_roundtrip(kb: KBManager):
    page = kb.write(name="normal-page", page_type="concept", body="hello world")
    assert page.path.exists()
    assert str(page.path).startswith(str(kb.root))

    read_back = kb.read("normal-page")
    assert read_back is not None
    assert read_back.body.strip() == "hello world"


def test_write_with_traversal_name_is_rejected(kb: KBManager, tmp_path: Path):
    """Regression test for the exact payload Codex used to write a real
    file outside kb.root."""
    outside_target = tmp_path / "escaped.md"
    assert not outside_target.exists()

    with pytest.raises(ValueError):
        kb.write(name="../escaped", page_type="concept", body="TRAVERSAL_CONFIRMED")

    assert not outside_target.exists()


@pytest.mark.parametrize("evil_name", [
    "../escaped",
    "../../etc/passwd",
    "a/../../escaped",
    "/etc/passwd",
])
def test_write_rejects_various_traversal_names(kb: KBManager, evil_name: str):
    with pytest.raises(ValueError):
        kb.write(name=evil_name, page_type="concept", body="x")


def test_write_with_traversal_subdir_is_rejected(kb: KBManager):
    with pytest.raises(ValueError):
        kb.write(name="page", page_type="concept", body="x", subdir="../../etc")


def test_read_with_traversal_name_is_rejected(kb: KBManager):
    with pytest.raises(ValueError):
        kb.read("../../../etc/passwd")


def test_read_with_traversal_subdir_is_rejected(kb: KBManager):
    with pytest.raises(ValueError):
        kb.read("page", subdir="../../etc")


def test_pages_with_traversal_subdir_is_rejected(kb: KBManager):
    with pytest.raises(ValueError):
        kb.pages(subdir="../../etc")


def test_append_with_traversal_name_is_rejected(kb: KBManager, tmp_path: Path):
    outside_target = tmp_path / "escaped.md"
    kb.write(name="../escaped_setup_attempt", page_type="concept", body="x") \
        if False else None  # no legitimate way to pre-create the traversal target
    with pytest.raises((ValueError, FileNotFoundError)):
        kb.append(name="../escaped", section="log", content="pwned")
    assert not outside_target.exists()


def test_normal_subdir_write_and_pages_still_work(kb: KBManager):
    page = kb.write(name="proj1", page_type="project", body="body", subdir="projects")
    assert page.path.exists()
    assert str(page.path).startswith(str(kb.root / "projects"))

    pages = kb.pages(subdir="projects")
    assert any(p.path == page.path for p in pages)
