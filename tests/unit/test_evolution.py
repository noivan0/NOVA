"""tests/unit/test_evolution.py — Unit tests for EvolutionLog."""
import tempfile
from nova.core.evolution import EvolutionLog


def test_record_and_retrieve():
    with tempfile.TemporaryDirectory() as tmp:
        evo = EvolutionLog(tmp)
        evo.record(
            run_id="run_001",
            harness="research",
            pattern="pipeline",
            started_at="2026-01-01T00:00:00Z",
            success=True,
            duration_secs=42.0,
            quality_score=85,
            phases_run=["web_search", "synthesis"],
        )
        entries = evo.recent(5)
        assert len(entries) == 1
        assert entries[0]["success"] is True
        assert entries[0]["quality_score"] == 85


def test_failure_rate():
    with tempfile.TemporaryDirectory() as tmp:
        evo = EvolutionLog(tmp)
        for i in range(10):
            evo.record(
                run_id=f"run_{i:03d}",
                harness="test",
                pattern="pipeline",
                started_at="2026-01-01T00:00:00Z",
                success=(i % 2 == 0),  # 5 success, 5 fail
                duration_secs=10.0,
            )
        rate = evo.failure_rate(10)
        assert abs(rate - 0.5) < 0.01


def test_consecutive_failures():
    with tempfile.TemporaryDirectory() as tmp:
        evo = EvolutionLog(tmp)
        for i in range(3):
            evo.record(
                run_id=f"run_{i:03d}",
                harness="test",
                pattern="pipeline",
                started_at="2026-01-01T00:00:00Z",
                success=False,
                duration_secs=5.0,
            )
        assert evo.consecutive_failures() == 3
