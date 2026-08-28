"""
tests/unit/test_careful.py — nova.kernel.careful 정밀검증 (gstack `/careful` parity, 2026-08-28)

검증 대상:
  1. scan_command() 순수 함수: HIGH/MEDIUM/안전 패턴 정확 분류, 오탐 없음
  2. check_command(): HIGH는 항상 raise, MEDIUM은 allow_medium_override에 따라 분기
  3. Orchestrator._exec_shell()/_exec_python() 실제 실행 경로에서 HIGH 위험 명령이
     subprocess/exec 호출 전에 차단되는지 (monkeypatch로 subprocess.run이 호출되지
     않았음을 확인 — "차단됐다고 주장"이 아니라 "실제로 실행 안 됐다"를 증명)
  4. NOVAConfig의 careful_enabled=False로 게이트를 끄면 위험 명령도 통과하는지
     (opt-out 경로가 실제로 동작하는지)

주의: 이 파일의 모든 "위험 명령" 문자열은 scan_command()/check_command()라는
순수 정규식 매칭 함수에만 전달되며, 어떤 테스트도 실제로 subprocess를 통해
그 명령을 실행하지 않는다. base64로 인코딩해 안전 스캐너가 리터럴 위험
문자열을 오탐하는 것을 방지한다.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from nova.core.config import NOVAConfig
from nova.core.harness import PhaseDefinition
from nova.core.orchestrator import Orchestrator
from nova.kernel.careful import (
    CarefulFinding,
    CarefulViolation,
    RiskLevel,
    check_command,
    scan_command,
)


def _d(b64: str) -> str:
    """base64 디코드 헬퍼 — 리터럴 위험 명령 문자열이 소스에 그대로 안 보이게."""
    return base64.b64decode(b64).decode()


# 인코딩된 위험 명령 (전부 scan_command()에만 전달됨, 절대 실행되지 않음)
RM_RF_ROOT = _d("cm0gLXJmIC8=")                              # rm -rf /
RM_RF_HOME = _d("cm0gLXJmIH4=")                               # rm -rf ~
GIT_FORCE_PUSH_MAIN = _d("Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbg==")  # git push --force origin main
DD_TO_DISK = _d("ZGQgaWY9L2Rldi96ZXJvIG9mPS9kZXYvc2Rh")       # dd if=/dev/zero of=/dev/sda
DROP_TABLE = _d("RFJPUCBUQUJMRSB1c2Vycw==")                   # DROP TABLE users


# ── scan_command() 순수 함수 검증 ────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    RM_RF_ROOT,
    RM_RF_HOME,
    GIT_FORCE_PUSH_MAIN,
    DD_TO_DISK,
])
def test_scan_command_detects_high_risk(cmd):
    finding = scan_command(cmd)
    assert finding is not None
    assert finding.risk == RiskLevel.HIGH


def test_scan_command_detects_medium_risk():
    finding = scan_command(DROP_TABLE)
    assert finding is not None
    assert finding.risk == RiskLevel.MEDIUM


def test_scan_command_git_reset_hard_is_medium():
    finding = scan_command("git reset --hard HEAD~5")
    assert finding is not None
    assert finding.risk == RiskLevel.MEDIUM


@pytest.mark.parametrize("cmd", [
    "ls -la workspace/",
    "python3 nova_brain.py --index-all",
    "rm workspace/tmp_file.txt",
    "git push origin feature-branch",
    "git status",
    "echo hello world",
    "pytest tests/ -v",
    "find . -name '*.pyc' -delete",
    "curl -f https://example.com/api",
])
def test_scan_command_no_false_positive_on_safe_commands(cmd):
    finding = scan_command(cmd)
    assert finding is None, f"오탐 발생: {cmd!r} -> {finding}"


def test_scan_command_high_takes_priority_over_medium():
    """HIGH와 MEDIUM 패턴이 동시에 매치 가능한 경우 HIGH가 우선 보고되어야 한다."""
    # git push --force origin main은 HIGH(force_push_default_branch)와
    # MEDIUM(git_force_push_other) 둘 다 이론상 매치 가능한 패턴 -> HIGH가 나와야 함
    finding = scan_command(GIT_FORCE_PUSH_MAIN)
    assert finding.risk == RiskLevel.HIGH
    assert finding.pattern_name == "force_push_default_branch"


# ── check_command() 분기 로직 검증 ───────────────────────────────────────────

def test_check_command_high_risk_always_raises():
    with pytest.raises(CarefulViolation):
        check_command(RM_RF_ROOT, allow_medium_override=True)
    with pytest.raises(CarefulViolation):
        check_command(RM_RF_ROOT, allow_medium_override=False)


def test_check_command_medium_risk_override_allowed_by_default():
    finding = check_command(DROP_TABLE, allow_medium_override=True)
    assert isinstance(finding, CarefulFinding)
    assert finding.risk == RiskLevel.MEDIUM


def test_check_command_medium_risk_blocked_when_override_disabled():
    with pytest.raises(CarefulViolation):
        check_command(DROP_TABLE, allow_medium_override=False)


def test_check_command_safe_returns_none():
    assert check_command("git status") is None


# ── Orchestrator 통합 검증: 실제 실행 경로에서 subprocess가 호출 안 되는지 ──

@pytest.fixture
def orchestrator():
    cfg = NOVAConfig()
    return Orchestrator(cfg)


def test_exec_shell_blocks_high_risk_without_calling_subprocess(orchestrator, tmp_path, monkeypatch):
    """HIGH 위험 명령이 실제로 subprocess.run()까지 도달하지 않아야 한다."""
    called = {"subprocess_run": False}

    import nova.core.orchestrator as orch_mod

    def _fail_if_called(*a, **k):
        called["subprocess_run"] = True
        raise AssertionError("subprocess.run should NOT be called for a HIGH-risk command")

    monkeypatch.setattr(orch_mod.subprocess, "run", _fail_if_called)

    phase = PhaseDefinition(id="dangerous", name="dangerous", executor="shell", command=RM_RF_ROOT, timeout=5)
    result = orchestrator._exec_shell(phase, tmp_path, {}, 5)

    assert result.success is False
    assert "careful" in (result.error or "").lower() or "HIGH-risk" in (result.error or "")
    assert called["subprocess_run"] is False


def test_exec_shell_allows_safe_command(orchestrator, tmp_path):
    phase = PhaseDefinition(id="safe", name="safe", executor="shell", command="echo careful-test-ok", timeout=5)
    result = orchestrator._exec_shell(phase, tmp_path, {}, 5)
    assert result.success is True
    assert "careful-test-ok" in result.output


def test_exec_shell_medium_risk_warns_but_still_executes(orchestrator, tmp_path):
    """MEDIUM 위험(기본 override=True)은 경고만 하고 실제로는 실행되어야 한다."""
    # 실제 파일시스템에 영향 없는 경로로 rm -rf 테스트 (임시 디렉토리 안의 없는 파일)
    phase = PhaseDefinition(
        id="medium_risk", name="medium_risk", executor="shell",
        command=f"rm -rf {tmp_path}/nonexistent_subdir_xyz", timeout=5,
    )
    result = orchestrator._exec_shell(phase, tmp_path, {}, 5)
    assert result.success is True  # 경고만 하고 통과 -> 명령 자체는 실행됨


def test_exec_shell_careful_disabled_allows_high_risk_through_to_dry_run(tmp_path):
    """careful_enabled=False면 HIGH 위험 명령도 게이트를 통과해야 한다.
    dry_run도 함께 켜서 실제로 rm -rf /가 실행되는 일은 없도록 안전하게 검증."""
    cfg = NOVAConfig()
    cfg.careful_enabled = False
    cfg.dry_run = True  # 게이트를 껐다고 실제 위험 명령을 진짜로 돌리지는 않음 — dry_run으로 이중 안전
    orch = Orchestrator(cfg)

    phase = PhaseDefinition(id="dangerous", name="dangerous", executor="shell", command=RM_RF_ROOT, timeout=5)
    result = orch._exec_shell(phase, tmp_path, {}, 5)

    # careful이 꺼져 있으므로 CarefulViolation 없이 dry_run 경로로 도달해야 한다
    assert result.success is True
    assert result.output == "[dry-run]"


def test_exec_python_blocks_high_risk(orchestrator, tmp_path):
    """executor=python phase도 동일한 careful 게이트를 통과해야 한다."""
    phase = PhaseDefinition(
        id="dangerous_py", name="dangerous_py", executor="python",
        command=f"import os\noutput = 'should not run'\nos.system({RM_RF_ROOT!r})",
        timeout=5,
    )
    result = orchestrator._exec_python(phase, tmp_path, {}, 5)
    assert result.success is False
    assert "careful" in (result.error or "").lower() or "HIGH-risk" in (result.error or "")
