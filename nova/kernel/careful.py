"""
nova.kernel.careful — Destructive-command safety gate (gstack `/careful` parity)
==================================================================================

harness.yaml의 ``executor: shell`` / ``executor: python`` phase는 orchestrator
프로세스 권한으로 임의 명령을 실행한다 (nova/core/orchestrator.py
``_exec_shell`` / ``_exec_python``). harness 작성자의 실수(오타, 잘못 붙여넣은
경로 변수 등) 하나가 그대로 ``rm -rf``, ``DROP TABLE``, ``git push --force``
같은 파괴적 명령으로 이어질 수 있는데, 지금까지 이를 걸러내는 장치가 전혀
없었다.

gstack(garrytan/gstack)의 ``/careful`` 스킬이 하는 일을 결정론적 코드로
재현한다:
  - HIGH  위험: 루트/홈 재귀삭제, default-branch force-push 등 — 하드 차단
    (CarefulViolation 발생, 설정으로도 우회 불가)
  - MEDIUM 위험: 일반 rm -rf, DROP TABLE, git reset --hard 등 — 경고만 하고
    통과 (override 가능, gstack의 "Say be careful to activate. Override any
    MEDIUM warning" 동작과 동일)

이 모듈은 순수 함수(외부 상태 없음)이므로 단독 테스트가 쉽고, orchestrator
외의 다른 실행 경로(nova_codex_gate.py의 셸 호출 등)에도 재사용 가능하다.
"""

from __future__ import annotations

import dataclasses
import re
from enum import Enum


class RiskLevel(str, Enum):
    """감지된 위험 수준."""
    NONE = "none"
    MEDIUM = "medium"   # 경고 후 통과 가능
    HIGH = "high"        # 하드 차단, 우회 불가


@dataclasses.dataclass
class CarefulFinding:
    """단일 위험 패턴 매치 결과."""
    pattern_name: str
    risk: RiskLevel
    matched_text: str
    reason: str


class CarefulViolation(Exception):
    """HIGH 위험 명령이 감지되어 실행이 차단되었을 때 발생."""

    def __init__(self, finding: CarefulFinding, command: str) -> None:
        self.finding = finding
        self.command = command
        super().__init__(
            f"[careful] HIGH-risk command blocked: {finding.pattern_name} "
            f"({finding.reason}) — command: {command[:200]!r}"
        )


# ── 위험 패턴 정의 ────────────────────────────────────────────────────────────
#
# 순서 중요: HIGH를 먼저 검사해 가장 위험한 패턴이 우선 매치되도록 한다.
# 정규식은 shell/python 두 실행기 모두에서 쓰이는 명령 문자열에 대해 동작하며,
# 대소문자 무시(re.IGNORECASE)로 매치한다.

_HIGH_RISK_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "rm_rf_root_or_home",
        re.compile(r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+(/|~|\$HOME|\$\{HOME\})(?:[\s'\"]|$|/)", re.IGNORECASE),
        "루트 또는 홈 디렉토리 전체 재귀삭제 — 복구 불가능",
    ),
    (
        "force_push_default_branch",
        re.compile(r"git\s+push\s+.*(?:--force(?:-with-lease)?|-f)\b\s+.*\b(origin\s+)?(main|master)\b", re.IGNORECASE),
        "default 브랜치 force-push — 팀 전체 히스토리 파괴 가능",
    ),
    (
        "dd_to_block_device",
        re.compile(r"\bdd\b.*of=/dev/(sd|nvme|hd)", re.IGNORECASE),
        "블록 디바이스에 직접 쓰기 — 디스크 전체 파괴 가능",
    ),
    (
        "mkfs_on_mounted",
        re.compile(r"\bmkfs(\.\w+)?\s+/dev/", re.IGNORECASE),
        "블록 디바이스 포맷 — 데이터 전체 손실",
    ),
]

_MEDIUM_RISK_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "rm_rf_generic",
        re.compile(r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s", re.IGNORECASE),
        "재귀 강제삭제 — 대상 경로를 다시 확인할 것",
    ),
    (
        "drop_table",
        re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
        "테이블 삭제 — 데이터 손실 가능",
    ),
    (
        "drop_database",
        re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
        "데이터베이스 삭제 — 데이터 손실 가능",
    ),
    (
        "git_reset_hard",
        re.compile(r"git\s+reset\s+--hard\b", re.IGNORECASE),
        "커밋되지 않은 변경사항 전체 폐기",
    ),
    (
        "git_force_push_other",
        re.compile(r"git\s+push\s+.*(?:--force(?:-with-lease)?|-f)\b", re.IGNORECASE),
        "force-push — 원격 히스토리 덮어씀 (default 브랜치 아님)",
    ),
    (
        "truncate_table",
        re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
        "테이블 전체 데이터 삭제",
    ),
    (
        "chmod_recursive_permissive",
        re.compile(r"chmod\s+(-R|--recursive)\s+(777|a\+rwx)", re.IGNORECASE),
        "재귀적 전체권한 부여 — 보안 위험",
    ),
]


def scan_command(command: str) -> CarefulFinding | None:
    """명령 문자열에서 가장 위험한 패턴 하나를 찾아 반환. 안전하면 None.

    HIGH 패턴을 MEDIUM보다 먼저 검사하므로, 두 종류가 동시에 매치되는
    명령이라도 항상 HIGH가 우선 보고된다.
    """
    for name, pattern, reason in _HIGH_RISK_PATTERNS:
        m = pattern.search(command)
        if m:
            return CarefulFinding(
                pattern_name=name,
                risk=RiskLevel.HIGH,
                matched_text=m.group(0),
                reason=reason,
            )
    for name, pattern, reason in _MEDIUM_RISK_PATTERNS:
        m = pattern.search(command)
        if m:
            return CarefulFinding(
                pattern_name=name,
                risk=RiskLevel.MEDIUM,
                matched_text=m.group(0),
                reason=reason,
            )
    return None


def check_command(command: str, *, allow_medium_override: bool = True) -> CarefulFinding | None:
    """명령을 검사하고 필요시 CarefulViolation을 raise한다.

    Parameters
    ----------
    command:
        검사할 셸 명령 또는 python 코드 문자열.
    allow_medium_override:
        True(기본값)면 MEDIUM 위험은 예외를 던지지 않고 finding만 반환
        (호출자가 로그에 경고만 남기고 계속 진행). False면 MEDIUM도 차단.
        HIGH는 이 값과 무관하게 항상 차단된다.

    Returns
    -------
    CarefulFinding | None
        위험이 감지됐지만 차단되지 않은 경우(MEDIUM + override 허용)의
        finding. 완전히 안전하면 None.

    Raises
    ------
    CarefulViolation
        HIGH 위험이거나, allow_medium_override=False인데 MEDIUM 위험일 때.
    """
    finding = scan_command(command)
    if finding is None:
        return None
    if finding.risk == RiskLevel.HIGH:
        raise CarefulViolation(finding, command)
    if finding.risk == RiskLevel.MEDIUM and not allow_medium_override:
        raise CarefulViolation(finding, command)
    return finding
