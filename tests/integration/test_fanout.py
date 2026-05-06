"""tests/integration/test_fanout.py — Fanout pattern integration tests."""
import tempfile
from pathlib import Path

from nova.core.config import KBConfig, LLMConfig, NotifierConfig, NOVAConfig, PublisherConfig
from nova.core.harness import HarnessLoader
from nova.core.orchestrator import Orchestrator

FANOUT_YAML = """\
name: fanout-test
description: "Fanout test harness"
version: "1.0.0"
pattern: fanout

phases:
  - id: branch_a
    executor: llm
    prompt: "Write one sentence about topic A: {{topic}}"
    output_file: branch_a.md
    on_failure: skip

  - id: branch_b
    executor: llm
    prompt: "Write one sentence about topic B: {{topic}}"
    output_file: branch_b.md
    on_failure: skip

  - id: branch_c
    executor: shell
    command: "echo branch_c_done"
    output_file: branch_c.txt
    on_failure: skip

evolution:
  enabled: false
"""

FANOUT_ABORT_YAML = """\
name: fanout-abort
description: "Fanout abort test"
version: "1.0.0"
pattern: fanout

phases:
  - id: ok_phase
    executor: python
    command: "output = 'ok'"
    on_failure: skip

  - id: fail_phase
    executor: python
    command: "raise RuntimeError('forced failure')"
    on_failure: abort

  - id: never_reached
    executor: python
    command: "output = 'should not run'"
    on_failure: skip

evolution:
  enabled: false
"""


def _cfg(tmp: str) -> NOVAConfig:
    return NOVAConfig(
        workspace=str(Path(tmp) / "workspace"),
        harnesses_dir=str(Path(tmp) / "harnesses"),
        llm=LLMConfig(provider="echo"),
        notifier=NotifierConfig(provider="none"),
        publisher=PublisherConfig(provider="none"),
        kb=KBConfig(path=str(Path(tmp) / "kb")),
    )


def test_fanout_all_succeed():
    with tempfile.TemporaryDirectory() as tmp:
        hdir = Path(tmp) / "harnesses" / "fanout-test"
        hdir.mkdir(parents=True)
        (hdir / "harness.yaml").write_text(FANOUT_YAML)

        cfg = _cfg(tmp)
        loader = HarnessLoader(cfg.harnesses_dir)
        harness = loader.load("fanout-test")
        orch = Orchestrator(cfg)
        ok = orch.run(harness, context={"topic": "AI"}, resume=False)

        assert ok is True
        ws = Path(cfg.workspace) / "fanout-test"
        assert (ws / "branch_a.md").exists()
        assert (ws / "branch_b.md").exists()
        assert (ws / "branch_c.txt").exists()

        # _fanout_results should be in context — check indirectly via files
        assert "AI" in (ws / "branch_a.md").read_text()


def test_fanout_abort_on_failure():
    """Fanout with on_failure=abort should stop immediately when a phase fails."""
    with tempfile.TemporaryDirectory() as tmp:
        hdir = Path(tmp) / "harnesses" / "fanout-abort"
        hdir.mkdir(parents=True)
        (hdir / "harness.yaml").write_text(FANOUT_ABORT_YAML)

        cfg = _cfg(tmp)
        loader = HarnessLoader(cfg.harnesses_dir)
        harness = loader.load("fanout-abort")
        orch = Orchestrator(cfg)
        ok = orch.run(harness, context={}, resume=False)

        assert ok is False
        # never_reached phase should not have run — no output file
        # (there's no output_file on never_reached, so just assert run returned False)
