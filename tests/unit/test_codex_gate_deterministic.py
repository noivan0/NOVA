"""
tests/unit/test_codex_gate_deterministic.py — nova_codex_gate.py Deterministic-First
게이트 정밀검증 (2026-08-28)

nova_codex_gate.py는 nova/agents/bin/ 과 nova/agents/scripts/ 두 곳에 거의 동일한
구현이 있다 (bin=v2 병렬버전, scripts=v2.0 GPT-L2 버전). 둘 다 동일한
deterministic_checks() 사전검증 함수와, run_gate()가 그 결과로 LLM 호출을
생략하고 즉시 ABORT를 반환하는지 검증한다.

핵심 검증 포인트:
  - deterministic_checks()가 순수 함수로서 외부 API 호출 없이 판정
  - run_gate()가 deterministic 실패 시 claude_review/gpt_audit을 호출하지
    않고 즉시 ABORT를 반환 (LLM 비용/지연 회피 + 낙관적 승인 리스크 차단)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module", autouse=True)
def _isolate_env_from_dotenv_autoload(tmp_path_factory):
    """nova_codex_gate.py는 import 시점에 (HERMES_HOME / ".env")를 자동
    로드하고 os.environ.setdefault()로 실제 운영 환경변수
    (NOVA_LLM_PROVIDER=hmg 등)를 프로세스 전역에 주입한다. 이는 실제 운영
    설정으로는 올바르지만, 테스트 프로세스 안에서 그대로 두면 이후 실행되는
    다른 테스트(예: test_config.py)를 오염시킨다 — os.environ은 프로세스
    전역이라 pytest 세션 전체에 누수된다.

    HERMES_HOME을 빈 임시 디렉토리로 돌려 .env 자동로드가 아무 것도 하지
    않게 만들고 테스트가 끝나면 완전히 복원한다.
    """
    import os as _os

    tmp_hermes_home = tmp_path_factory.mktemp("fake_hermes_home")
    saved_env = dict(_os.environ)
    _os.environ["HERMES_HOME"] = str(tmp_hermes_home)
    yield
    _os.environ.clear()
    _os.environ.update(saved_env)


@pytest.fixture(scope="module")
def gate_scripts():
    return _load_module(
        REPO_ROOT / "nova" / "agents" / "scripts" / "nova_codex_gate.py",
        "nova_codex_gate_scripts_test",
    )


@pytest.fixture(scope="module")
def gate_bin():
    return _load_module(
        REPO_ROOT / "nova" / "agents" / "bin" / "nova_codex_gate.py",
        "nova_codex_gate_bin_test",
    )


# ── deterministic_checks() 순수 함수 검증 (scripts 버전) ────────────────────

def test_empty_content_fails(gate_scripts):
    passed, reasons = gate_scripts.deterministic_checks("proj", "")
    assert passed is False
    assert "empty_content" in reasons


def test_whitespace_only_content_fails(gate_scripts):
    passed, reasons = gate_scripts.deterministic_checks("proj", "   \n\t  ")
    assert passed is False
    assert "empty_content" in reasons


def test_too_short_content_fails(gate_scripts):
    passed, reasons = gate_scripts.deterministic_checks("proj", "short")
    assert passed is False
    assert any("content_too_short" in r for r in reasons)


def test_placeholder_marker_fails(gate_scripts):
    content = "This looks like a real article body but has a TODO left inside it by mistake here."
    passed, reasons = gate_scripts.deterministic_checks("proj", content)
    assert passed is False
    assert any("placeholder_markers_found" in r for r in reasons)


def test_html_only_no_real_text_fails(gate_scripts):
    passed, reasons = gate_scripts.deterministic_checks("proj", "<div><span></span><p>   </p></div>")
    assert passed is False
    assert "no_real_text_after_html_strip" in reasons


def test_normal_content_passes(gate_scripts):
    content = "This is a perfectly normal article with enough real content to pass all deterministic checks easily."
    passed, reasons = gate_scripts.deterministic_checks("proj", content)
    assert passed is True
    assert reasons == []


# ── run_gate()가 LLM 호출 없이 즉시 ABORT 하는지 (scripts 버전) ─────────────

def test_run_gate_scripts_aborts_on_empty_content_without_llm_call(gate_scripts, monkeypatch):
    """빈 콘텐츠면 claude_review가 아예 호출되지 않아야 한다."""
    called = {"claude_review": False, "gpt_audit": False}

    def _fail_if_called(*a, **k):
        called["claude_review"] = True
        raise AssertionError("claude_review should not be called when deterministic gate fails")

    monkeypatch.setattr(gate_scripts, "claude_review", _fail_if_called)

    result = gate_scripts.run_gate("test-proj", "", mode="review")

    assert result["verdict"] == "ABORT"
    assert result["final_source"] == "deterministic_gate_abort"
    assert result["claude"]["status"] == "skipped"
    assert result["codex"]["status"] == "skipped"
    assert called["claude_review"] is False


def test_run_gate_scripts_aborts_on_placeholder_content(gate_scripts, monkeypatch):
    def _fail_if_called(*a, **k):
        raise AssertionError("claude_review should not be called")

    monkeypatch.setattr(gate_scripts, "claude_review", _fail_if_called)

    content = "TODO: write the real article content here later, this is just a stub for now honestly."
    result = gate_scripts.run_gate("test-proj", content, mode="review")
    assert result["verdict"] == "ABORT"
    assert result["final_source"] == "deterministic_gate_abort"


# ── run_gate()가 LLM 호출 없이 즉시 ABORT 하는지 (bin 버전) ─────────────────

def test_run_gate_bin_aborts_on_empty_content_without_llm_call(gate_bin, monkeypatch):
    def _fail_if_called(*a, **k):
        raise AssertionError("claude_review should not be called when deterministic gate fails")

    monkeypatch.setattr(gate_bin, "claude_review", _fail_if_called)

    result = gate_bin.run_gate("test-proj", "", mode="review")

    assert result["verdict"] == "ABORT"
    assert result["merge_source"] == "deterministic_gate_abort"
    assert result["claude"]["status"] == "skipped"
    assert result["gpt"]["status"] == "skipped"


def test_bin_and_scripts_deterministic_checks_agree(gate_bin, gate_scripts):
    """두 구현이 동일한 입력에 대해 동일한 판정을 내려야 한다 (드리프트 방지)."""
    cases = [
        "",
        "short",
        "This is a perfectly normal article with enough real content to pass all deterministic checks easily.",
        "<div><span></span></div>",
    ]
    for content in cases:
        p_bin, _ = gate_bin.deterministic_checks("proj", content)
        p_scripts, _ = gate_scripts.deterministic_checks("proj", content)
        assert p_bin == p_scripts, f"bin/scripts disagree on: {content!r}"
