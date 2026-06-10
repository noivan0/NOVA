"""Tests for nova.wiki.synthesize — all phases without LLM."""

import tempfile
from pathlib import Path

import pytest

from nova.wiki.synthesize import (
    phase_crosslink,
    phase_index,
    phase_lessons,
    phase_stale,
    phase_takes,
)


def _make_wiki(root: Path) -> None:
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "concepts").mkdir(parents=True, exist_ok=True)
    (root / "entities" / "topic-a.md").write_text(
        "# Topic A\n\nSee also [[topic-b]] and [[topic-c]].\n", encoding="utf-8"
    )
    (root / "entities" / "topic-b.md").write_text(
        "# Topic B\n\nRelated to [[topic-a]].\n", encoding="utf-8"
    )
    (root / "concepts" / "topic-c.md").write_text(
        "# Topic C\n\nStandalone concept.\n", encoding="utf-8"
    )


class TestCrosslink:

    def test_crosslink_adds_backlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir)
            _make_wiki(wiki)
            updated = phase_crosslink(wiki, dry_run=False)
            assert updated > 0
            content = (wiki / "entities" / "topic-b.md").read_text()
            assert "## Backlinks" in content
            assert "topic-a" in content

    def test_crosslink_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir)
            _make_wiki(wiki)
            original = (wiki / "entities" / "topic-b.md").read_text()
            phase_crosslink(wiki, dry_run=True)
            assert (wiki / "entities" / "topic-b.md").read_text() == original

    def test_crosslink_missing_wiki_root(self):
        result = phase_crosslink(Path("/nonexistent/wiki"), dry_run=False)
        assert result == 0


class TestIndex:

    def test_index_creates_index_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir)
            _make_wiki(wiki)
            count = phase_index(wiki, dry_run=False)
            assert count > 0
            index = (wiki / "index.md").read_text()
            assert "topic-a" in index
            assert "topic-b" in index

    def test_index_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir)
            _make_wiki(wiki)
            phase_index(wiki, dry_run=True)
            assert not (wiki / "index.md").exists()


class TestLessons:

    def test_lessons_index_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir)
            kb = Path(tmpdir) / "kb"
            lessons = kb / "lessons"
            lessons.mkdir(parents=True)
            (lessons / "lesson-ssl.md").write_text("# SSL cert expiry\nDetail here.", encoding="utf-8")
            (lessons / "lesson-auth.md").write_text("# Auth pitfalls\nDetail here.", encoding="utf-8")

            count = phase_lessons(wiki, kb, dry_run=False)
            assert count == 2
            idx = (wiki / "concepts" / "lessons-index.md").read_text()
            assert "lesson-ssl" in idx
            assert "lesson-auth" in idx

    def test_lessons_no_lessons_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = phase_lessons(Path(tmpdir) / "wiki", Path(tmpdir) / "kb")
            assert result == 0


class TestTakes:

    def test_takes_no_brain_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = phase_takes(
                Path(tmpdir) / "wiki",
                Path(tmpdir) / "brain.db",
                dry_run=True,
            )
            assert result == 0

    def test_takes_with_db(self):
        from nova.db.brain import BrainDB

        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = Path(tmpdir) / "wiki"
            brain_path = Path(tmpdir) / "brain.db"
            db = BrainDB(brain_path)
            db.init()
            db.add_take("SSL certs expire", holder="test")
            db.add_take("Vector search scales", holder="test")

            result = phase_takes(wiki, brain_path, dry_run=False)
            assert result == 1
            summary = (wiki / "entities" / "nova-brain-takes-summary.md").read_text()
            assert "SSL certs expire" in summary


class TestStale:

    def test_stale_no_wiki(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = phase_stale(
                Path(tmpdir) / "wiki",
                Path(tmpdir) / "kb",
            )
            assert result == 0
