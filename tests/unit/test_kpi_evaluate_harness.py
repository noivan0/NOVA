"""
tests/unit/test_kpi_evaluate_harness.py — kpi_evaluate harness 정밀검증 (2026-08-28)

검증 대상:
  1. harness.yaml이 정상 파싱되고 3-Judge phase(Planner/Critic/Architect)가
     전부 로드되는지
  2. panel_verdict의 "Deterministic-First" 판정 로직을
     (harness.yaml 내 인라인 스크립트를 그대로 재현하여) 검증:
     - deterministic_pass=False면 LLM 만장일치 PASS라도 panel_pass=False
     - deterministic_pass=True + 2/3 이상 PASS면 panel_pass=True
     - deterministic_pass=True + 과반 미달이면 panel_pass=False
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nova.core.harness import HarnessLoader


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_DIR = REPO_ROOT / "harnesses" / "kpi_evaluate"


def test_kpi_evaluate_harness_parses():
    """harness.yaml이 정상 파싱되고 phase 목록에 3-judge가 전부 있어야 한다."""
    loader = HarnessLoader(harnesses_dir=str(REPO_ROOT / "harnesses"))
    hd = loader.load("kpi_evaluate")
    phase_ids = [p.id for p in hd.phases]
    assert "deterministic_gate" in phase_ids
    assert "evaluate_kpi" in phase_ids          # planner (claude)
    assert "evaluate_kpi_codex" in phase_ids    # critic (gpt)
    assert "evaluate_kpi_architect" in phase_ids  # architect (claude, dissent role)
    assert "panel_verdict" in phase_ids
    assert "dod_verify" in phase_ids


def test_architect_prompt_file_loads():
    """evaluate_kpi_architect phase의 prompt_file이 실제로 로드되어야 한다."""
    loader = HarnessLoader(harnesses_dir=str(REPO_ROOT / "harnesses"))
    hd = loader.load("kpi_evaluate")
    arch_phase = next(p for p in hd.phases if p.id == "evaluate_kpi_architect")
    assert len(arch_phase.prompt) > 100
    assert "KPI_PASS" in arch_phase.prompt
    assert "KPI_FAIL" in arch_phase.prompt


def test_panel_judges_metadata_has_three_roles():
    """harness.yaml의 panel_judges 메타데이터에 3개 역할이 명시되어야 한다."""
    raw = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())
    judges = raw.get("panel_judges", [])
    roles = {j.get("role") for j in judges}
    assert roles == {"planner", "critic", "architect"}


# ── Deterministic-First 판정 로직 재현 테스트 ────────────────────────────────
#
# panel_verdict phase는 harness.yaml 안의 인라인 Python 스크립트로 구현되어
# 있어 직접 import할 수 없다. 여기서는 그 판정 규칙을 독립 함수로 재현해
# 회귀를 방지한다 (harness.yaml을 고칠 때 이 로직도 함께 갱신 필요).

def _panel_verdict(deterministic_pass: bool, votes: list[str]) -> tuple[bool, str]:
    """panel_verdict phase의 판정 규칙을 재현한 순수 함수."""
    pass_votes = sum(1 for v in votes if v == "PASS")
    total_votes = len(votes)
    if not deterministic_pass:
        return False, "deterministic_gate_failed"
    if total_votes == 0:
        return False, "no_judge_votes"
    panel_pass = pass_votes >= 2 and (pass_votes / total_votes) >= 0.66
    return panel_pass, "llm_panel_vote"


def test_deterministic_gate_overrides_unanimous_llm_pass():
    """LLM 3/3 만장일치 PASS라도 deterministic_pass=False면 최종 FAIL이어야 한다."""
    panel_pass, reason = _panel_verdict(False, ["PASS", "PASS", "PASS"])
    assert panel_pass is False
    assert reason == "deterministic_gate_failed"


def test_deterministic_pass_with_majority_llm_pass_succeeds():
    """deterministic 통과 + LLM 2/3 이상 PASS면 최종 PASS."""
    panel_pass, reason = _panel_verdict(True, ["PASS", "PASS", "FAIL"])
    assert panel_pass is True
    assert reason == "llm_panel_vote"


def test_deterministic_pass_with_minority_llm_pass_fails():
    """deterministic 통과했지만 LLM 과반 미달이면 최종 FAIL."""
    panel_pass, reason = _panel_verdict(True, ["PASS", "FAIL", "FAIL"])
    assert panel_pass is False
    assert reason == "llm_panel_vote"


def test_no_judge_votes_fails_safe():
    """judge 투표가 하나도 없으면 안전하게 FAIL 처리되어야 한다."""
    panel_pass, reason = _panel_verdict(True, [])
    assert panel_pass is False
    assert reason == "no_judge_votes"


def test_unanimous_llm_fail_fails_regardless_of_deterministic():
    """LLM 전원 FAIL이면 deterministic 통과 여부와 무관하게 FAIL."""
    panel_pass, _ = _panel_verdict(True, ["FAIL", "FAIL", "FAIL"])
    assert panel_pass is False
