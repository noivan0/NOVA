"""Tests for nova.db.brain and nova.db.schema."""

import tempfile
from pathlib import Path

import pytest

from nova.db.brain import BrainDB
from nova.db.schema import BRAIN_SCHEMA


class TestBrainDB:

    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = BrainDB(Path(self._tmpdir.name) / "brain.db")
        self.db.init()

    def teardown_method(self):
        self._tmpdir.cleanup()

    def test_init_creates_tables(self):
        snap = self.db.snapshot()
        assert snap is not None
        assert snap["takes"] == 0
        assert snap["orphan"] == 0
        assert snap["health"] == 100.0

    def test_add_take_returns_id(self):
        tid = self.db.add_take("the sky is blue", holder="test-agent", weight=0.8)
        assert len(tid) == 16

    def test_snapshot_reflects_takes(self):
        self.db.add_take("claim A")
        self.db.add_take("claim B")
        snap = self.db.snapshot()
        assert snap["takes"] == 2

    def test_recent_takes_order(self):
        self.db.add_take("first claim", holder="a")
        self.db.add_take("second claim", holder="b")
        takes = self.db.recent_takes(n=10)
        assert len(takes) == 2
        # Most recent first
        assert takes[0]["claim"] == "second claim"

    def test_recent_takes_filtered_by_holder(self):
        self.db.add_take("claim by A", holder="agent-a")
        self.db.add_take("claim by B", holder="agent-b")
        takes = self.db.recent_takes(n=10, holder="agent-a")
        assert all(t["holder"] == "agent-a" for t in takes)
        assert len(takes) == 1

    def test_push_event_and_read(self):
        self.db.push_event("TEST_EVENT", "INFO", "test title", "some detail")
        events = self.db.unread_events()
        assert any(e["event_type"] == "TEST_EVENT" for e in events)

    def test_unread_events_marked_read(self):
        self.db.push_event("EV1", "INFO", "first")
        self.db.push_event("EV2", "HIGH", "second")
        first_read = self.db.unread_events()
        assert len(first_read) == 2
        # Should be empty now
        second_read = self.db.unread_events()
        assert len(second_read) == 0

    def test_record_health(self):
        self.db.add_take("something")
        self.db.record_health(95.5)
        snap = self.db.snapshot()
        assert snap is not None  # DB still readable

    def test_add_take_truncates_long_claims(self):
        long_claim = "x" * 1000
        tid = self.db.add_take(long_claim)
        takes = self.db.recent_takes(n=1)
        assert len(takes[0]["claim"]) <= 500

    def test_idempotent_init(self):
        """Calling init() twice should not raise."""
        self.db.init()
        self.db.init()
        assert self.db.snapshot() is not None


class TestSchema:

    def test_schema_creates_all_tables(self):
        import sqlite3
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = sqlite3.connect(f.name)
            db.executescript(BRAIN_SCHEMA)
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        assert "pages" in tables
        assert "takes" in tables
        assert "contradictions" in tables
        assert "brain_health" in tables
        assert "nova_events" in tables
