"""tests/unit/test_orchestrator_shell_injection.py — regression test for
Orchestrator._exec_shell()'s command injection vulnerability.

SECURITY-003 (2026-08-18, deep audit): {{var}} placeholders in a harness's
shell-executor `command` string were substituted with str(v) raw and the
result run under subprocess.run(..., shell=True). The `context` dict is
NOT harness-author-controlled -- it is populated from CLI `--context
key=value` flags (`nova run <harness> --context topic=...`), so it is
attacker-controlled input at runtime. Reproduced real arbitrary command
execution end-to-end via the CLI:

    nova run evil_test --context 'topic=world; touch /tmp/pwned; echo x'

which actually created /tmp/pwned on disk. Fixed by shell-quoting every
substituted value with shlex.quote() so it is always treated as a single
opaque argument, never as shell syntax.
"""
import tempfile
from pathlib import Path

import pytest

from nova.core.config import NOVAConfig
from nova.core.harness import PhaseDefinition
from nova.core.orchestrator import Orchestrator


@pytest.fixture
def orchestrator(tmp_path: Path) -> Orchestrator:
    cfg = NOVAConfig()
    cfg.llm.provider = "echo"
    cfg.kb.path = str(tmp_path / "kb")
    cfg.workspace = str(tmp_path / "workspace")
    return Orchestrator(cfg)


def test_shell_injection_via_context_is_neutralized(orchestrator: Orchestrator, tmp_path: Path):
    """Regression test for the exact payload that produced a real file on
    disk before the fix."""
    marker = tmp_path / "pwned_marker"
    assert not marker.exists()

    phase = PhaseDefinition(
        id="greet", name="Greet", executor="shell",
        command="echo hello {{topic}}",
    )
    malicious_context = {"topic": f"world; touch {marker}; echo injected"}

    result = orchestrator._exec_shell(
        phase, workspace=tmp_path, context=malicious_context, timeout=10,
    )

    assert not marker.exists(), (
        "shell injection payload executed — {{var}} substitution is not "
        "shell-quoting attacker-controlled context values"
    )
    # The whole malicious string should appear as a literal argument to
    # `echo`, not be interpreted as shell syntax.
    assert "injected" in result.output or "world" in result.output


def test_normal_context_value_still_substitutes_correctly(orchestrator: Orchestrator, tmp_path: Path):
    phase = PhaseDefinition(
        id="greet", name="Greet", executor="shell",
        command="echo hello {{topic}}",
    )
    result = orchestrator._exec_shell(
        phase, workspace=tmp_path, context={"topic": "world"}, timeout=10,
    )
    assert result.success
    assert "hello" in result.output
    assert "world" in result.output


@pytest.mark.parametrize("payload", [
    "$(touch /tmp/should_not_exist_1)",
    "`touch /tmp/should_not_exist_2`",
    "; touch /tmp/should_not_exist_3 #",
    "&& touch /tmp/should_not_exist_4",
    "| touch /tmp/should_not_exist_5",
])
def test_various_shell_metacharacter_payloads_are_neutralized(
    orchestrator: Orchestrator, tmp_path: Path, payload: str,
):
    for marker_num in range(1, 6):
        Path(f"/tmp/should_not_exist_{marker_num}").unlink(missing_ok=True)

    phase = PhaseDefinition(
        id="greet", name="Greet", executor="shell",
        command="echo hello {{topic}}",
    )
    orchestrator._exec_shell(
        phase, workspace=tmp_path, context={"topic": payload}, timeout=10,
    )

    for marker_num in range(1, 6):
        p = Path(f"/tmp/should_not_exist_{marker_num}")
        assert not p.exists(), f"payload {payload!r} executed via metachar {marker_num}"
        p.unlink(missing_ok=True)


def test_chained_placeholder_substitution_cannot_bypass_quoting(
    orchestrator: Orchestrator, tmp_path: Path,
):
    """SECURITY-006 (2026-08-18, Codex-audited round 2): the first
    shlex.quote() fix looped `for k, v in context.items(): cmd =
    cmd.replace(...)`, applying each substitution to the ALREADY-
    substituted string. If one context value's raw text happens to
    contain another key's literal placeholder (e.g. context={"a":
    "{{b}}", "b": "; rm -rf ~"}), inserting the quoted "a" value first
    plants a literal "{{b}}" string into the command, which the *next*
    iteration then matches and substitutes with "b"'s raw (dangerous)
    value -- reinjecting shell syntax inside what looked like a safely
    quoted region. Codex reproduced this end-to-end (a real file was
    created via this exact chained substitution before the fix). Fixed
    by doing a single non-recursive re.sub() pass over the ORIGINAL
    command string, so a substituted value's contents are never
    re-scanned for further placeholders.
    """
    marker = tmp_path / "pwned_via_chain"
    assert not marker.exists()

    phase = PhaseDefinition(
        id="greet", name="Greet", executor="shell",
        command="echo {{a}} {{b}}",
    )
    # 'a' carries a literal placeholder for 'b'; 'b' carries the payload.
    chained_context = {
        "a": "{{b}}",
        "b": f"; touch {marker}; echo chained",
    }

    result = orchestrator._exec_shell(
        phase, workspace=tmp_path, context=chained_context, timeout=10,
    )

    assert not marker.exists(), (
        "chained placeholder substitution bypassed shlex.quote() and "
        "executed the injected payload"
    )
    # The literal string "{{b}}" must appear verbatim in the output --
    # it must NOT have been re-substituted with b's raw value.
    assert "{{b}}" in result.output


def test_reflexive_self_referencing_placeholder_is_safe(
    orchestrator: Orchestrator, tmp_path: Path,
):
    """A context value that names its own key as a placeholder (a
    degenerate case of the chaining bug) must not cause infinite
    substitution or any command execution."""
    marker = tmp_path / "pwned_self_ref"
    phase = PhaseDefinition(
        id="greet", name="Greet", executor="shell",
        command="echo {{a}}",
    )
    self_referencing_context = {"a": f"{{{{a}}}}; touch {marker}"}

    orchestrator._exec_shell(
        phase, workspace=tmp_path, context=self_referencing_context, timeout=10,
    )
    assert not marker.exists()

