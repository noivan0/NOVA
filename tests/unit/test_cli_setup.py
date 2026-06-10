"""Tests for nova.cli setup and watcher commands."""

import tempfile
from pathlib import Path

import pytest


class TestSetupCommand:

    def _run_setup(self, nova_home: str) -> str:
        """Run `nova setup` and return captured stdout."""
        import io
        import sys
        from unittest.mock import patch
        
        captured = io.StringIO()
        argv = ["nova", "setup", "--nova-home", nova_home]
        with patch("sys.argv", argv), patch("sys.stdout", captured):
            try:
                from nova.cli.main import main
                main()
            except SystemExit:
                pass
        return captured.getvalue()

    def test_setup_creates_directory_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "test-nova"
            self._run_setup(str(nova_home))

            assert (nova_home / "brain.db").exists()
            assert (nova_home / "memory.md").exists()
            assert (nova_home / "kb").is_dir()
            assert (nova_home / "kb" / "lessons").is_dir()
            assert (nova_home / "wiki").is_dir()
            assert (nova_home / "kanban" / "boards").is_dir()
            assert (nova_home / "engines").is_dir()
            assert (nova_home / "logs").is_dir()

    def test_setup_installs_engines(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "test-nova"
            self._run_setup(str(nova_home))

            engines = list((nova_home / "engines").glob("*.py"))
            engine_names = {e.name for e in engines}
            assert "dream.py" in engine_names
            assert "learn.py" in engine_names
            assert "synthesize.py" in engine_names
            assert "chain.py" in engine_names
            assert "fix_orphan.py" in engine_names
            assert "memory_slim.py" in engine_names

    def test_setup_installs_wiki_synthesize(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "test-nova"
            self._run_setup(str(nova_home))
            assert (nova_home / "wiki" / "synthesize.py").exists()

    def test_setup_initialises_brain_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "test-nova"
            self._run_setup(str(nova_home))

            from nova.db.brain import BrainDB
            snap = BrainDB(nova_home / "brain.db").snapshot()
            assert snap is not None
            assert snap["takes"] == 0
            assert snap["health"] == 100.0

    def test_setup_idempotent(self):
        """Running setup twice should not raise or overwrite engines."""
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "test-nova"
            self._run_setup(str(nova_home))
            # Modify an engine file to confirm it is not overwritten
            engine = nova_home / "engines" / "dream.py"
            engine.write_text("# custom", encoding="utf-8")
            self._run_setup(str(nova_home))
            # Custom content preserved (setup skips existing files)
            assert engine.read_text() == "# custom"


class TestWatcherStatusCommand:

    def _run_watcher_status(self, nova_home: str) -> str:
        import io
        import sys
        from unittest.mock import patch

        captured = io.StringIO()
        argv = ["nova", "watcher", "status", "--nova-home", nova_home]
        with patch("sys.argv", argv), patch("sys.stdout", captured):
            try:
                from nova.cli.main import main
                main()
            except SystemExit:
                pass
        return captured.getvalue()

    def test_status_shows_not_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp) / "nova"
            # Run setup first
            from nova.cli.main import _cmd_setup

            class FakeArgs:
                nova_home_str = str(nova_home)
                install_engines = True

            class _A:
                pass
            a = _A()
            a.nova_home = str(nova_home)
            a.install_engines = True
            _cmd_setup(a)

            output = self._run_watcher_status(str(nova_home))
            assert "NOT STARTED" in output or "STOPPED" in output

    def test_status_shows_brain_db_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            nova_home_path = Path(tmp) / "nova"
            nova_home_str = str(nova_home_path)
            from nova.cli.main import _cmd_setup

            class FakeArgs2:
                nova_home = nova_home_str
                install_engines = False

            _cmd_setup(FakeArgs2())

            from nova.db.brain import BrainDB
            db = BrainDB(nova_home_path / "brain.db")
            db.add_take("test claim", holder="test", weight=0.9)

            output = self._run_watcher_status(nova_home_str)
            assert "brain.db" in output
            assert "takes=1" in output
