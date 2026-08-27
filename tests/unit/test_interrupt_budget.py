"""
tests/unit/test_interrupt_budget.py — InterruptRouter.classify_with_budget()
정밀검증 (2026-08-28)

oh-my-hermes의 "요청별 필요한 만큼만 예산 내 투영, 나머지는 사유와 함께
명시적 배제" 원칙을 InterruptRouter에 적용한 기능 검증.

검증 대상:
  1. budget보다 인터럽트 후보가 적으면 전부 selected, excluded는 빈 리스트
  2. budget보다 인터럽트 후보가 많으면 우선순위 상위 N개만 selected,
     나머지는 excluded에 명시적 reason과 함께 담김
  3. budget=0이면 selected가 비고 전부 excluded
  4. budget 미지정 시 domain_routing.yaml의 defaults.interrupt_budget 값 사용
  5. 기존 classify()는 하위호환으로 그대로 동작 (전체 목록 반환, 배제 개념 없음)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from nova.kernel.interrupt import BudgetedClassification, InterruptKind, InterruptRouter


ROUTING_YAML = str(_REPO_ROOT / "nova" / "kernel" / "domain_routing.yaml")


def make_takes(claims: list[str], kind: str = "fact", holder: str = "hermes") -> list[dict]:
    return [{"kind": kind, "claim": c, "holder": holder, "recorded_at": "2026-08-28T00:00:00"} for c in claims]


def _multi_domain_takes() -> list[dict]:
    """여러 도메인이 동시에 매칭되도록 구성한 takes — DOMAIN_RESEARCH +
    SELF_HEAL 두 종류의 인터럽트가 동시에 뜨도록 함."""
    return make_takes([
        # example_ops_monitoring 도메인 (min_matches=2)
        "cycle time anomaly detected on equipment line",
        "throughput dashboard shows downtime spike",
        # system 도메인 (min_matches=3, BUG 패턴)
        "BUG: nova_bridge에서 ImportError 발생",
        "에러: brain.db 연결 timeout 반복 — 수정 필요",
        "FAIL: orchestrator.run() returncode != 0 — 실패 패턴",
    ])


def test_budget_larger_than_candidates_selects_all():
    """예산이 충분하면 전부 selected, excluded는 비어야 한다."""
    router = InterruptRouter(ROUTING_YAML)
    takes = _multi_domain_takes()
    result = router.classify_with_budget(takes, budget=10)
    assert isinstance(result, BudgetedClassification)
    all_candidates = router.classify(takes)
    assert len(result.selected) == len(all_candidates)
    assert result.excluded == []
    assert result.budget == 10


def test_budget_smaller_than_candidates_excludes_with_reason():
    """예산 초과분은 excluded에 명시적 reason과 함께 담겨야 한다."""
    router = InterruptRouter(ROUTING_YAML)
    takes = _multi_domain_takes()
    all_candidates = router.classify(takes)
    assert len(all_candidates) >= 2, "테스트 전제: 인터럽트 후보 2개 이상 필요"

    result = router.classify_with_budget(takes, budget=1)
    assert len(result.selected) == 1
    assert len(result.excluded) == len(all_candidates) - 1
    for excl in result.excluded:
        assert "reason" in excl and excl["reason"], "배제 사유가 비어있으면 안 됨"
        assert "budget_exceeded" in excl["reason"]
        assert "kind" in excl and "domain" in excl and "harness" in excl

    # selected가 우선순위 최상위와 일치해야 함
    assert result.selected[0].kind == all_candidates[0].kind
    assert result.selected[0].domain == all_candidates[0].domain


def test_budget_zero_excludes_everything():
    """budget=0이면 selected가 비고, 후보가 있었다면 전부 excluded로 이동."""
    router = InterruptRouter(ROUTING_YAML)
    takes = _multi_domain_takes()
    all_candidates = router.classify(takes)
    result = router.classify_with_budget(takes, budget=0)
    assert result.selected == []
    assert len(result.excluded) == len(all_candidates)


def test_default_budget_from_yaml_defaults():
    """budget 인자를 생략하면 domain_routing.yaml의 defaults.interrupt_budget을 쓴다."""
    router = InterruptRouter(ROUTING_YAML)
    takes = _multi_domain_takes()
    result = router.classify_with_budget(takes)  # budget 생략
    # domain_routing.yaml에 interrupt_budget: 1 로 설정되어 있음
    assert result.budget == 1
    assert len(result.selected) <= 1


def test_no_candidates_empty_selected_and_excluded():
    """인터럽트 후보가 아예 없으면 selected/excluded 모두 비어야 한다."""
    router = InterruptRouter(ROUTING_YAML)
    takes = make_takes(["평범한 대화 한 줄"])  # 아무 도메인도 안 걸림, GENERALIZE도 미달
    result = router.classify_with_budget(takes, budget=5)
    assert result.selected == []
    assert result.excluded == []


def test_classify_still_works_unchanged_for_backward_compat():
    """기존 classify()는 예산 개념 없이 그대로 전체 목록을 반환해야 한다."""
    router = InterruptRouter(ROUTING_YAML)
    takes = _multi_domain_takes()
    result = router.classify(takes)
    assert isinstance(result, list)
    assert all(hasattr(i, "kind") for i in result)
    # BudgetedClassification이 아니라 순수 list[Interrupt]
    assert not isinstance(result, BudgetedClassification)
