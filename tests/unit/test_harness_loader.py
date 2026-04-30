"""tests/unit/test_harness_loader.py — Unit tests for HarnessLoader."""
import tempfile
from pathlib import Path
from nova.core.harness import HarnessLoader


SAMPLE_HARNESS = """\
name: test-harness
description: "A test harness"
version: "1.0.0"
pattern: pipeline

phases:
  - id: step_1
    name: "Step 1"
    executor: llm
    prompt: "Write a poem about {topic}."
    output_file: poem.md
    on_failure: retry

  - id: step_2
    name: "Step 2"
    executor: shell
    command: "echo done"
    on_failure: abort

runbook:
  - symptom: "timeout"
    action: "notify"
    escalate_after: 300

evolution:
  enabled: true
"""


def test_load_harness():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "test-harness"
        d.mkdir()
        (d / "harness.yaml").write_text(SAMPLE_HARNESS)

        loader = HarnessLoader(tmp)
        harness = loader.load("test-harness")

        assert harness.name == "test-harness"
        assert harness.pattern == "pipeline"
        assert len(harness.phases) == 2
        assert harness.phases[0].id == "step_1"
        assert harness.phases[1].executor == "shell"
        assert len(harness.runbook) == 1
        assert harness.runbook[0].symptom == "timeout"


def test_list_harnesses():
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("alpha", "beta", "gamma"):
            d = Path(tmp) / name
            d.mkdir()
            (d / "harness.yaml").write_text(f"name: {name}\nphases: []\n")

        loader = HarnessLoader(tmp)
        found = loader.list_harnesses()
        assert set(found) == {"alpha", "beta", "gamma"}


def test_load_missing_raises():
    with tempfile.TemporaryDirectory() as tmp:
        loader = HarnessLoader(tmp)
        try:
            loader.load("nonexistent")
            assert False, "Should have raised"
        except FileNotFoundError:
            pass
