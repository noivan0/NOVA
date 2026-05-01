"""tests/unit/test_python_executor.py — Tests for Python phase executor."""
import tempfile
from pathlib import Path

from nova.core.config import NOVAConfig, LLMConfig, NotifierConfig, PublisherConfig, KBConfig
from nova.core.harness import HarnessDefinition, PhaseDefinition
from nova.core.orchestrator import Orchestrator


def _make_orch(tmp: str) -> Orchestrator:
    cfg = NOVAConfig(
        workspace=str(Path(tmp) / "workspace"),
        harnesses_dir=str(Path(tmp) / "harnesses"),
        llm=LLMConfig(provider="echo"),
        notifier=NotifierConfig(provider="none"),
        publisher=PublisherConfig(provider="none"),
        kb=KBConfig(path=str(Path(tmp) / "kb")),
    )
    return Orchestrator(cfg)


def _phase(phase_id: str, **kwargs) -> PhaseDefinition:
    return PhaseDefinition(id=phase_id, name=phase_id, **kwargs)


def test_python_phase_sets_output():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orch(tmp)
        harness = HarnessDefinition(
            name="py-test",
            phases=[
                _phase(
                    "compute",
                    executor="python",
                    command="output = str(2 + 2)",
                    output_file="result.txt",
                    on_failure="abort",
                )
            ],
        )
        ok = orch.run(harness, context={}, resume=False)
        assert ok is True
        result_file = Path(orch.config.workspace) / "py-test" / "result.txt"
        assert result_file.exists()
        assert result_file.read_text() == "4"


def test_python_phase_failure_propagates():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orch(tmp)
        harness = HarnessDefinition(
            name="py-fail",
            phases=[
                _phase(
                    "crash",
                    executor="python",
                    command="raise ValueError('intentional failure')",
                    on_failure="abort",
                )
            ],
        )
        ok = orch.run(harness, context={}, resume=False)
        assert ok is False


def test_python_phase_accesses_context():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _make_orch(tmp)
        harness = HarnessDefinition(
            name="py-ctx",
            phases=[
                _phase(
                    "use-ctx",
                    executor="python",
                    command="output = context.get('greeting', 'default')",
                    output_file="msg.txt",
                    on_failure="abort",
                )
            ],
        )
        ok = orch.run(harness, context={"greeting": "hello"}, resume=False)
        assert ok is True
        msg = (Path(orch.config.workspace) / "py-ctx" / "msg.txt").read_text()
        assert msg == "hello"


def test_python_phase_timeout():
    """Python phase should fail gracefully on timeout (SIGALRM, Linux only)."""
    import platform
    if platform.system() == "Windows":
        return  # SIGALRM not available

    with tempfile.TemporaryDirectory() as tmp:
        cfg = NOVAConfig(
            workspace=str(Path(tmp) / "workspace"),
            harnesses_dir=str(Path(tmp) / "harnesses"),
            llm=LLMConfig(provider="echo"),
            notifier=NotifierConfig(provider="none"),
            publisher=PublisherConfig(provider="none"),
            kb=KBConfig(path=str(Path(tmp) / "kb")),
            phase_timeout=1,
        )
        orch = Orchestrator(cfg)
        harness = HarnessDefinition(
            name="py-timeout",
            phases=[
                _phase(
                    "infinite-loop",
                    executor="python",
                    command="import time; time.sleep(999)",
                    timeout=1,
                    on_failure="abort",
                )
            ],
        )
        ok = orch.run(harness, context={}, resume=False)
        assert ok is False
