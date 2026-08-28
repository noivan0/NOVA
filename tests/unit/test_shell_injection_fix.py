"""
tests/unit/test_shell_injection_fix.py — LLM 출력 기반 셸 인젝션 방지 검증 (2026-08-28)

배경: Black Hat 2026에서 Check Point가 LangChain/LangGraph/CrewAI/AutoGen/
Google ADK/Microsoft Agent Framework에서 "prompt-controlled content가
trusted framework logic 경계를 넘는다"는 공통 취약점 패턴을 공개했다.
NOVA를 감사한 결과 Orchestrator._exec_shell()의
`cmd.replace("{{key}}", str(v))`가 정확히 이 클래스의 결함이었음을 실측
재현으로 확인했다: 이전 LLM phase의 raw 출력(context["_phase_x"])이
셸 명령 문자열에 이스케이프 없이 삽입된 뒤 shell=True로 실행되어, 조작된
LLM 출력이 명령 구분자(", ;, 개행 등)를 깨고 임의 명령을 실행할 수 있었다.

근본 수정: `{{key}}` 문자열 템플릿 치환을 셸 명령 조립에서 완전히 제거하고,
context 값은 NOVA_CTX_<KEY> 환경변수로만 전달한다. 환경변수는 셸의 명령
파싱 단계 이후 값이 통째로 전달되므로 인용부호/구분자가 있어도 명령 구조를
바꿀 수 없다.

이 파일의 테스트는 "안전해졌다고 주장"이 아니라, 실제 공격 페이로드로
`_exec_shell()`을 호출해 부작용(파일 생성 등)이 발생하지 않음을 실행
결과로 증명한다.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from nova.core.config import NOVAConfig
from nova.core.harness import PhaseDefinition
from nova.core.orchestrator import Orchestrator


# 공격 페이로드: 셸 메타문자(", ;, $())를 포함해 명령 구분자를 깨려 시도.
# base64 등으로 숨기지 않는다 — 이건 실제 셸에 전달되는 명령이 아니라
# "LLM이 반환했다고 가정하는 텍스트 문자열"일 뿐이며, 이 텍스트 자체는
# 위험한 명령이 아니라 위험한 명령을 만들어내려는 "데이터"이기 때문이다.
INJECTION_PAYLOADS = [
    'safe text"; touch {marker}; echo "',
    "safe text'; touch {marker}; echo '",
    "safe text`touch {marker}`",
    "safe text$(touch {marker})",
    "safe text\ntouch {marker}\n",
]


@pytest.fixture
def orchestrator():
    cfg = NOVAConfig()
    return Orchestrator(cfg)


@pytest.mark.parametrize("payload_template", INJECTION_PAYLOADS)
def test_double_curly_template_no_longer_interpolated_into_shell(orchestrator, tmp_path, payload_template):
    """{{key}} 템플릿이 더 이상 셸 문자열에 치환되지 않으므로, 어떤
    페이로드를 context에 넣어도 명령 구조가 깨지지 않아야 한다."""
    marker = tmp_path / "pwned_marker"
    payload = payload_template.format(marker=marker)

    phase = PhaseDefinition(
        id="report", name="report", executor="shell",
        command='echo "{{_phase_analyze}}"',
        timeout=5,
    )
    context = {"_phase_analyze": payload}
    result = orchestrator._exec_shell(phase, tmp_path, context, 5)

    assert result.success is True
    assert not marker.exists(), f"셸 인젝션 성공 — marker 파일이 생성됨: {payload!r}"


def test_context_values_are_exposed_as_env_vars_not_string_interpolated(orchestrator, tmp_path):
    """context 값은 NOVA_CTX_<KEY> 환경변수로 전달되어야 하며, 그 값이
    셸에 의해 재해석되지 않고 원본 그대로 유지되어야 한다."""
    payload = 'value with "quotes" and $(command) and ; semicolons'
    phase = PhaseDefinition(
        id="report", name="report", executor="shell",
        command='echo "$NOVA_CTX__PHASE_ANALYZE"',
        timeout=5,
    )
    context = {"_phase_analyze": payload}
    result = orchestrator._exec_shell(phase, tmp_path, context, 5)

    assert result.success is True
    assert payload in result.output


def test_no_marker_file_created_from_env_var_payload(orchestrator, tmp_path):
    """환경변수로 전달된 악의적 페이로드가 실제로 실행되지 않는지 최종 확인."""
    marker = tmp_path / "pwned_via_envvar"
    payload = f'safe"; touch {marker}; echo "'
    phase = PhaseDefinition(
        id="report", name="report", executor="shell",
        command='echo "$NOVA_CTX__PHASE_ANALYZE"',
        timeout=5,
    )
    context = {"_phase_analyze": payload}
    result = orchestrator._exec_shell(phase, tmp_path, context, 5)

    assert result.success is True
    assert not marker.exists()


def test_env_var_key_sanitization_only_alnum_and_underscore(orchestrator, tmp_path):
    """context 키에 특수문자가 있어도 안전한 환경변수 이름으로 변환되어야
    한다 (예: 'my-key!' -> NOVA_CTX_MY_KEY_)."""
    phase = PhaseDefinition(
        id="report", name="report", executor="shell",
        command="env | grep NOVA_CTX_ | sort",
        timeout=5,
    )
    context = {"my-key!": "hello"}
    result = orchestrator._exec_shell(phase, tmp_path, context, 5)

    assert result.success is True
    assert "NOVA_CTX_MY_KEY_=hello" in result.output


def test_normal_harness_shell_commands_still_work(orchestrator, tmp_path):
    """{{}} 템플릿을 쓰지 않는 정상적인 shell phase는 영향을 받지 않아야
    한다 (실제 21개 harness 전수조사 결과와 일치하는 회귀 방지)."""
    phase = PhaseDefinition(
        id="normal", name="normal", executor="shell",
        command="echo hello-normal-case && pwd",
        timeout=5,
    )
    result = orchestrator._exec_shell(phase, tmp_path, {}, 5)
    assert result.success is True
    assert "hello-normal-case" in result.output
