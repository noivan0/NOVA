"""
test_interrupt.py — InterruptRouter 단위 테스트 (Phase 2)
==========================================================

테스트 항목:
  1. MMS 키워드 takes → DOMAIN_RESEARCH(mms) 반환
  2. BUG 키워드 takes → SELF_HEAL 반환
  3. takes 미달 → 인터럽트 없음 (폴백 미발동)
  4. takes 20개 이상 (도메인 미매칭) → SYNTHESIZE 폴백
  5. should_trigger() — 쿨다운 중 False 반환
  6. route() — 도메인 설정 harness 반환
"""

import sys
import os
import time
from pathlib import Path

# Make sure the repo's own `nova` package is importable when this test file
# is run directly (python tests/test_interrupt.py) as well as under pytest.
# P1 fix (2026-08-18): this used to hardcode `Path.home() / "nova"`, which
# only worked on the original author's personal machine (where a checkout
# happened to live at ~/nova). On every other machine/CI runner it silently
# missed the real domain_routing.yaml and the router fell back to
# "no domain_routing.yaml" behavior, causing test_mms_domain_research to
# fail regardless of the actual InterruptRouter implementation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from nova.kernel.interrupt import InterruptKind, InterruptRouter


ROUTING_YAML = str(_REPO_ROOT / "nova" / "kernel" / "domain_routing.yaml")


def make_takes(claims: list[str], kind: str = "fact", holder: str = "hermes") -> list[dict]:
    """테스트용 takes 목록 생성."""
    return [{"kind": kind, "claim": c, "holder": holder, "recorded_at": "2026-07-16T00:00:00"} for c in claims]


def test_mms_domain_research():
    """MMS 키워드 takes → DOMAIN_RESEARCH(mms) 반환 확인."""
    router = InterruptRouter(ROUTING_YAML)
    takes = make_takes([
        "T-GDI 세타3 엔진 CT 분석 결과 OP11 사이클타임 이상",
        "MMS CNC 데이터에서 T15 가공 오류 감지",
        "OP14 세타 블록라인 공정 이상 감지",
    ])
    interrupts = router.classify(takes)
    assert interrupts, "인터럽트가 반환되어야 함"
    kinds = [i.kind for i in interrupts]
    domains = [i.domain for i in interrupts]
    assert InterruptKind.DOMAIN_RESEARCH in kinds, f"DOMAIN_RESEARCH 없음 — {kinds}"
    mms_idx = kinds.index(InterruptKind.DOMAIN_RESEARCH)
    assert domains[mms_idx] == "mms", f"도메인이 'mms'여야 함 — {domains[mms_idx]}"
    print(f"  ✅ MMS DOMAIN_RESEARCH: confidence={interrupts[mms_idx].confidence:.2f}, harness={interrupts[mms_idx].harness}")


def test_self_heal():
    """BUG 키워드 takes 3개 이상 → SELF_HEAL 반환 확인."""
    router = InterruptRouter(ROUTING_YAML)
    takes = make_takes([
        "BUG: nova_bridge _maybe_run_harness에서 ImportError 발생",
        "에러: brain.db 연결 timeout 반복 — 수정 필요",
        "FAIL: orchestrator.run() returncode != 0 — 실패 패턴",
        "버그: takes 기록 후 nudge 미전달 문제",
    ])
    interrupts = router.classify(takes)
    assert interrupts, "인터럽트가 반환되어야 함"
    kinds = [i.kind for i in interrupts]
    assert InterruptKind.SELF_HEAL in kinds, f"SELF_HEAL 없음 — {kinds}"
    sh_idx = kinds.index(InterruptKind.SELF_HEAL)
    assert interrupts[sh_idx].domain == "system", f"도메인이 'system'이어야 함"
    print(f"  ✅ SELF_HEAL: confidence={interrupts[sh_idx].confidence:.2f}, harness={interrupts[sh_idx].harness}")


def test_no_interrupt_below_threshold():
    """키워드 1개로 min_matches 미달 → DOMAIN_RESEARCH 미발동 확인."""
    router = InterruptRouter(ROUTING_YAML)
    takes = make_takes([
        "세타3 관련 일반 대화",  # MMS 키워드 1개뿐
    ])
    interrupts = router.classify(takes)
    domain_researches = [i for i in interrupts if i.kind == InterruptKind.DOMAIN_RESEARCH]
    mms_hits = [i for i in domain_researches if i.domain == "mms"]
    assert not mms_hits, f"min_matches 미달인데 MMS 인터럽트 발동됨: {mms_hits}"
    print(f"  ✅ min_matches 미달 — MMS 인터럽트 없음 (전체 인터럽트: {[i.kind.value for i in interrupts]})")


def test_synthesize_fallback():
    """도메인 미매칭 + GENERALIZE 미발동 + takes 20개 이상 → SYNTHESIZE 폴백 확인."""
    router = InterruptRouter(ROUTING_YAML)
    # kind, holder를 다양하게 해 GENERALIZE 발동 방지 (3회 미만 조합)
    takes = [
        {"kind": f"kind_{i % 8}", "claim": f"일반 대화 내용 {i}: 특정 도메인 키워드 없음",
         "holder": f"agent_{i % 7}", "recorded_at": "2026-07-16T00:00:00"}
        for i in range(22)
    ]
    interrupts = router.classify(takes)
    assert interrupts, "인터럽트가 반환되어야 함 (SYNTHESIZE 폴백)"
    kinds = [i.kind for i in interrupts]
    assert InterruptKind.SYNTHESIZE in kinds, f"SYNTHESIZE 없음 — {kinds}"
    sy_idx = kinds.index(InterruptKind.SYNTHESIZE)
    assert interrupts[sy_idx].domain == "general", f"domain이 'general'이어야 함"
    print(f"  ✅ SYNTHESIZE 폴백: takes {len(takes)}개 → harness={interrupts[sy_idx].harness}")


def test_should_trigger_cooldown():
    """쿨다운 중인 harness → should_trigger() == False 확인."""
    router = InterruptRouter(ROUTING_YAML)
    from nova.kernel.interrupt import Interrupt, InterruptKind

    intr = Interrupt(
        kind=InterruptKind.DOMAIN_RESEARCH,
        domain="mms",
        confidence=0.8,
        evidence=["세타3 OP11 이상"],
        harness="research",
        priority=2,
    )
    # 방금 트리거한 상태 (쿨다운 내)
    last_triggered = {"research": time.time() - 30}  # 30초 전 실행 (쿨다운 60분)
    result = router.should_trigger(intr, last_triggered)
    assert not result, "쿨다운 중에 True가 반환됨"
    print(f"  ✅ 쿨다운 중 should_trigger=False (30초 전 실행, 쿨다운 60분)")

    # 쿨다운 이후 (오래된 타임스탬프)
    last_triggered_old = {"research": time.time() - 7200}  # 2시간 전
    result_old = router.should_trigger(intr, last_triggered_old)
    assert result_old, "쿨다운 지났는데 False가 반환됨"
    print(f"  ✅ 쿨다운 완료 후 should_trigger=True (2시간 전 실행)")


def test_route_returns_correct_harness():
    """route() — mms 도메인 → domain_routing.yaml에 설정된 harness 반환.

    P1 fix (2026-08-18): domain_routing.yaml의 mms 도메인은 v3.0(nova/kernel/
    domain_routing.yaml 커밋 이력 참고)부터 전용 `mms_research` harness로
    라우팅하도록 바뀌었는데 이 테스트는 그 이전 값(`research`)을 그대로
    기대하고 있어 stale했다. route()는 domain_routing.yaml 설정을
    interrupt.harness보다 우선하므로(nova/kernel/interrupt.py route() 참고),
    실제 설정값(mms_research)에 맞춰 기대값을 갱신한다.
    """
    router = InterruptRouter(ROUTING_YAML)
    from nova.kernel.interrupt import Interrupt, InterruptKind

    intr = Interrupt(
        kind=InterruptKind.DOMAIN_RESEARCH,
        domain="mms",
        confidence=0.7,
        evidence=["T-GDI 세타3"],
        harness="research",  # domain_routing.yaml이 우선하므로 이 값은 무시됨
        priority=2,
    )
    harness = router.route(intr)
    assert harness == "mms_research", f"harness가 'mms_research'여야 함 — {harness}"
    print(f"  ✅ route(mms) → '{harness}'")


def test_fallback_without_yaml():
    """routing_yaml 없이 InterruptRouter 초기화 → 빈 도메인, SYNTHESIZE 폴백 작동."""
    router = InterruptRouter("/nonexistent/path.yaml")
    # kind/holder 다양하게 해 GENERALIZE 미발동
    takes = [
        {"kind": f"kind_{i % 8}", "claim": f"일반 대화 {i}",
         "holder": f"agent_{i % 7}", "recorded_at": "2026-07-16T00:00:00"}
        for i in range(25)
    ]
    interrupts = router.classify(takes)
    # YAML 없어도 SYNTHESIZE 폴백은 동작해야 함
    kinds = [i.kind for i in interrupts]
    assert InterruptKind.SYNTHESIZE in kinds, f"YAML 없어도 SYNTHESIZE 폴백 필요 — {kinds}"
    print(f"  ✅ YAML 없이 SYNTHESIZE 폴백 동작: {kinds}")


if __name__ == "__main__":
    tests = [
        ("MMS DOMAIN_RESEARCH",           test_mms_domain_research),
        ("BUG → SELF_HEAL",               test_self_heal),
        ("min_matches 미달 → 인터럽트 없음",   test_no_interrupt_below_threshold),
        ("SYNTHESIZE 폴백 (20개 이상)",       test_synthesize_fallback),
        ("쿨다운 should_trigger()",         test_should_trigger_cooldown),
        ("route() harness 확인",            test_route_returns_correct_harness),
        ("YAML 없이 폴백 동작",               test_fallback_without_yaml),
    ]

    passed = 0
    failed = 0
    print("\n=== NOVA Phase 2 — InterruptRouter 단위 테스트 ===\n")
    for name, fn in tests:
        print(f"[TEST] {name}")
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ ERROR: {type(e).__name__}: {e}")
            failed += 1
        print()

    print(f"=== 결과: {passed}/{len(tests)} 통과, {failed} 실패 ===")
    sys.exit(0 if failed == 0 else 1)
