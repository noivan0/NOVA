"""tests/unit/test_checkpoint.py — Unit tests for Checkpoint."""
import tempfile

from nova.core.checkpoint import Checkpoint


def test_start_and_resume():
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = Checkpoint(tmp)
        run_id = ckpt.start("test-harness", stale_threshold_secs=300)
        assert run_id.startswith("run_")

        saved = ckpt.resume()
        assert saved is not None
        assert saved["harness"] == "test-harness"
        assert saved["run_id"] == run_id


def test_update_and_complete():
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = Checkpoint(tmp)
        ckpt.start("test-harness")
        ckpt.update(2, "quality_check", {"city": "paris"})

        saved = ckpt.resume()
        assert saved["phase"] == 2
        assert saved["phase_id"] == "quality_check"
        assert saved["state"]["city"] == "paris"

        ckpt.complete()
        assert not ckpt.exists()
        assert ckpt.resume() is None


def test_stale_checkpoint_cleared():
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = Checkpoint(tmp)
        ckpt.start("test-harness", stale_threshold_secs=0)  # immediately stale
        saved = ckpt.resume()
        assert saved is None  # cleared because stale
