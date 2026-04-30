"""tests/integration/test_orchestrator_echo.py
Integration test using the Echo LLM provider (no API key needed).
"""
import tempfile
from pathlib import Path
from nova.core.config import NOVAConfig, LLMConfig, NotifierConfig, PublisherConfig, KBConfig
from nova.core.harness import HarnessLoader
from nova.core.orchestrator import Orchestrator


HARNESS_YAML = """\
name: echo-test
description: "Integration test harness using echo provider"
version: "1.0.0"
pattern: pipeline

phases:
  - id: generate
    executor: llm
    prompt: "Write a paragraph about {{topic}}."
    output_file: output.md
    on_failure: abort

  - id: shell_check
    executor: shell
    command: "test -f output.md && echo OK || echo MISSING"
    on_failure: skip

evolution:
  enabled: true
"""


def test_orchestrator_echo_provider():
    with tempfile.TemporaryDirectory() as tmp:
        # Create harness
        harness_dir = Path(tmp) / "harnesses" / "echo-test"
        harness_dir.mkdir(parents=True)
        (harness_dir / "harness.yaml").write_text(HARNESS_YAML)

        # Config with echo provider
        cfg = NOVAConfig(
            workspace=str(Path(tmp) / "workspace"),
            harnesses_dir=str(Path(tmp) / "harnesses"),
            llm=LLMConfig(provider="echo"),
            notifier=NotifierConfig(provider="none"),
            publisher=PublisherConfig(provider="none"),
            kb=KBConfig(path=str(Path(tmp) / "kb")),
        )

        loader = HarnessLoader(cfg.harnesses_dir)
        harness = loader.load("echo-test")

        orch = Orchestrator(cfg)
        ok = orch.run(harness, context={"topic": "space exploration"}, resume=False)

        assert ok is True

        # Check output was written
        output = Path(cfg.workspace) / "echo-test" / "output.md"
        assert output.exists()
        assert "[echo]" in output.read_text()
