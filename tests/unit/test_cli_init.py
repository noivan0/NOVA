"""tests/unit/test_cli_init.py — Unit tests for `nova init` and the packaged
harnesses fallback (P0 install fix, 2026-08-18).

Covers:
- HarnessLoader falls back to the package-bundled harnesses when cwd has no
  (or an empty) ./harnesses directory.
- HarnessLoader still prefers a real ./harnesses directory when one exists
  (backward compatibility for existing users / git clones).
- `nova init` copies bundled harnesses + nova.yaml into cwd.
- Security regression test: `nova init --harnesses ..` (or any name not in
  the enumerated whitelist) must never reach shutil.rmtree/copytree.
  Found by Codex audit 2026-08-18 — see
  kb/fixes/nova-oss-nova-init-path-traversal-audit-20260818.md
"""
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nova.cli.main import _cmd_init
from nova.core.harness import HarnessLoader, _packaged_harnesses_dir


def _chdir(tmp):
    """Context helper: chdir into tmp, always restore afterwards."""
    class _Ctx:
        def __enter__(self_inner):
            self_inner.prev = Path.cwd()
            os.chdir(tmp)
            return tmp

        def __exit__(self_inner, *exc):
            os.chdir(self_inner.prev)

    return _Ctx()


def test_packaged_harnesses_dir_exists_and_has_research():
    packaged = _packaged_harnesses_dir()
    assert packaged is not None
    assert packaged.exists()
    assert (packaged / "research" / "harness.yaml").exists()


def test_loader_falls_back_to_packaged_when_no_local_harnesses():
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        # No ./harnesses at all in this cwd — this is the exact fresh
        # `pip install` state that used to print "No harnesses found".
        loader = HarnessLoader("./harnesses")
        found = loader.list_harnesses()
        assert "research" in found
        assert "summarizer" in found


def test_loader_prefers_real_local_harnesses_dir():
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        local = Path(tmp) / "harnesses" / "legacy"
        local.mkdir(parents=True)
        (local / "harness.yaml").write_text("name: legacy\npattern: pipeline\nphases: []\n")

        loader = HarnessLoader("./harnesses")
        found = loader.list_harnesses()
        # Backward compat: an existing real harnesses/ dir must win over the
        # packaged fallback, and must NOT be silently merged with it.
        assert found == ["legacy"]


def test_cmd_init_copies_default_harnesses_and_nova_yaml():
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        _cmd_init(SimpleNamespace(all=False, harnesses=None, force=False))

        dest = Path(tmp) / "harnesses"
        assert (dest / "research" / "harness.yaml").exists()
        assert (dest / "summarizer" / "harness.yaml").exists()
        assert (dest / "data-pipeline" / "harness.yaml").exists()
        assert (Path(tmp) / "nova.yaml").exists()

        # The copied harness must actually be loadable end-to-end.
        loader = HarnessLoader("./harnesses")
        harness = loader.load("research")
        assert harness.name


def test_cmd_init_rejects_path_traversal_name():
    """Regression test for the Codex-found P0 security issue (2026-08-18):
    `nova init --harnesses ..` must be rejected by the whitelist and must
    never reach shutil.rmtree()/copytree() with an unsafe path.
    """
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        calls = []
        with patch("shutil.rmtree", side_effect=lambda p: calls.append(("rmtree", str(p)))), \
             patch("shutil.copytree", side_effect=lambda s, d: calls.append(("copytree", str(s), str(d)))):
            _cmd_init(SimpleNamespace(all=False, harnesses=[".."], force=True))

        assert calls == [], f"path traversal reached filesystem ops: {calls}"
        # cwd itself must obviously survive untouched.
        assert Path(tmp).exists()


def test_cmd_init_rejects_absolute_and_nested_traversal_names():
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        for unsafe in ("../../etc", "/etc/passwd", "../outside", "a/../../b"):
            calls = []
            with patch("shutil.rmtree", side_effect=lambda p: calls.append(("rmtree", str(p)))), \
                 patch("shutil.copytree", side_effect=lambda s, d: calls.append(("copytree", str(s), str(d)))):
                _cmd_init(SimpleNamespace(all=False, harnesses=[unsafe], force=True))
            assert calls == [], f"unsafe name {unsafe!r} reached filesystem ops: {calls}"


def test_cmd_init_force_overwrite_still_works_for_legit_name():
    with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
        _cmd_init(SimpleNamespace(all=False, harnesses=["research"], force=False))
        marker = Path(tmp) / "harnesses" / "research" / "MARKER"
        marker.write_text("stale")

        _cmd_init(SimpleNamespace(all=False, harnesses=["research"], force=True))

        # Legit re-copy with --force should still replace the directory
        # contents (the whitelist fix must not break normal --force usage).
        assert not marker.exists()
        assert (Path(tmp) / "harnesses" / "research" / "harness.yaml").exists()
