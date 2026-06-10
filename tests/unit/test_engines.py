"""Tests for nova.engine — all 6 built-in engines."""

import tempfile
from pathlib import Path

import pytest

from nova.db.brain import BrainDB


def _setup_nova_home(tmp: str) -> Path:
    """Create a minimal nova_home with brain.db."""
    nova_home = Path(tmp)
    db = BrainDB(nova_home / "brain.db")
    db.init()
    return nova_home


def _add_takes(db: BrainDB, n: int, holder: str = "test") -> None:
    for i in range(n):
        db.add_take(f"test claim {i} about knowledge and learning", holder=holder, weight=0.8)


class TestLearnEngine:

    def test_learn_no_brain_db(self):
        from nova.engine.learn import run
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))
            assert result == 0

    def test_learn_links_takes(self):
        from nova.engine.learn import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 5)
            # No KB pages → linked=0 but should not raise
            result = run(nova_home)
            assert isinstance(result, int)
            assert result >= 0

    def test_learn_with_kb_pages(self):
        from nova.engine.learn import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            # Insert a KB page and a matching take
            import uuid
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            pid = uuid.uuid4().hex[:16]
            brain_conn = __import__("sqlite3").connect(str(nova_home / "brain.db"))
            brain_conn.execute(
                "INSERT INTO pages (id, path, title, page_type, compiled_truth, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, "kb/test.md", "Test Page", "general",
                 "knowledge learning information test claim", now, now),
            )
            brain_conn.commit()
            brain_conn.close()
            _add_takes(db, 3)
            result = run(nova_home)
            assert isinstance(result, int)


class TestSynthesizeEngine:

    def test_synthesize_no_brain_db(self):
        from nova.engine.synthesize import run
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))
            assert result == 0

    def test_synthesize_writes_kb_page(self):
        from nova.engine.synthesize import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 5, holder="nova-research")
            result = run(nova_home)
            assert result > 0
            synthesis_dir = nova_home / "kb" / "synthesis"
            pages = list(synthesis_dir.glob("*.md"))
            assert len(pages) > 0

    def test_synthesize_groups_by_holder(self):
        from nova.engine.synthesize import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 3, holder="agent-a")
            _add_takes(db, 3, holder="agent-b")
            result = run(nova_home)
            assert result >= 2  # one page per holder


class TestDreamEngine:

    def test_dream_no_brain_db(self):
        from nova.engine.dream import run
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))
            assert result == 100.0

    def test_dream_records_health(self):
        from nova.engine.dream import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 5)
            health = run(nova_home)
            assert 0.0 <= health <= 100.0

    def test_dream_emits_event(self):
        from nova.engine.dream import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 3)
            run(nova_home)
            events = db.unread_events()
            dream_events = [e for e in events if e["event_type"] == "DREAM_DONE"]
            assert len(dream_events) > 0

    def test_dream_prune_flag(self):
        from nova.engine.dream import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            db = BrainDB(nova_home / "brain.db")
            _add_takes(db, 3)
            # Should not raise with prune=True
            health = run(nova_home, prune=True)
            assert health is not None


class TestFixOrphanEngine:

    def test_fix_orphan_no_brain_db(self):
        from nova.engine.fix_orphan import run
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))
            assert result == 0

    def test_fix_orphan_assigns_agent(self):
        from nova.engine.fix_orphan import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = _setup_nova_home(tmp)
            import sqlite3, uuid
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            db = sqlite3.connect(str(nova_home / "brain.db"))
            for i in range(3):
                pid = uuid.uuid4().hex[:16]
                db.execute(
                    "INSERT INTO pages (id, path, title, page_type, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (pid, f"kb/test-{i}.md", f"Test {i}", "general", now, now),
                )
            db.commit()
            db.close()
            result = run(nova_home)
            assert result == 3


class TestMemorySlimEngine:

    def test_memory_slim_skips_when_small(self):
        from nova.engine.memory_slim import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp)
            memory_md = nova_home / "memory.md"
            memory_md.write_text("§\nsmall content\n§\n", encoding="utf-8")
            result = run(nova_home)
            assert result is False

    def test_memory_slim_trims_when_large(self):
        from nova.engine.memory_slim import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp)
            (nova_home / "logs").mkdir()
            memory_md = nova_home / "memory.md"
            # Write sections that total >85% of limit (20,000 chars)
            section = "x" * 1000
            sections = [f"§\n{section}\n" for _ in range(20)]  # ~20,000 chars
            memory_md.write_text("".join(sections), encoding="utf-8")
            result = run(nova_home)
            assert result is True
            new_size = len(memory_md.read_text(encoding="utf-8"))
            assert new_size < 20000

    def test_memory_slim_archives_trimmed(self):
        from nova.engine.memory_slim import run
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp)
            (nova_home / "logs").mkdir()
            memory_md = nova_home / "memory.md"
            section = "y" * 1000
            memory_md.write_text(
                "\n".join([f"§\n{section}" for _ in range(20)]),
                encoding="utf-8",
            )
            run(nova_home)
            archive = nova_home / "logs" / "memory_archive.md"
            assert archive.exists()
            assert len(archive.read_text()) > 0


class TestChainEngine:

    def test_chain_no_boards(self):
        from nova.engine.chain import run
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))
            assert result == 0

    def test_chain_with_empty_board(self):
        from nova.engine.chain import run
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            nova_home = Path(tmp)
            board_dir = nova_home / "kanban" / "boards" / "test-board"
            board_dir.mkdir(parents=True)
            db = sqlite3.connect(str(board_dir / "kanban.db"))
            db.execute(
                "CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "agent TEXT, priority INTEGER, created_at TEXT, updated_at TEXT)"
            )
            db.commit()
            db.close()
            result = run(nova_home)
            assert result == 0
