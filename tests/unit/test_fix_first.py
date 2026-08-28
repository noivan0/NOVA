"""
tests/unit/test_fix_first.py — nova.kernel.fix_first 정밀검증 (gstack parity, 2026-08-28)
"""
from __future__ import annotations

from nova.kernel.fix_first import (
    FixFirstTier,
    classify_confidence,
    classify_findings,
)


def test_confidence_95_and_above_is_auto_fix():
    assert classify_confidence(95).tier == FixFirstTier.AUTO_FIX
    assert classify_confidence(96.5).tier == FixFirstTier.AUTO_FIX
    assert classify_confidence(100).tier == FixFirstTier.AUTO_FIX


def test_confidence_85_to_94_is_critical():
    assert classify_confidence(85).tier == FixFirstTier.CRITICAL
    assert classify_confidence(90).tier == FixFirstTier.CRITICAL
    assert classify_confidence(94.9).tier == FixFirstTier.CRITICAL


def test_confidence_below_85_is_informational():
    assert classify_confidence(84.9).tier == FixFirstTier.INFORMATIONAL
    assert classify_confidence(50).tier == FixFirstTier.INFORMATIONAL
    assert classify_confidence(0).tier == FixFirstTier.INFORMATIONAL


def test_confidence_as_percentage_string():
    r = classify_confidence("97%")
    assert r.tier == FixFirstTier.AUTO_FIX
    assert r.confidence == 97.0


def test_confidence_as_ratio_0_to_1():
    r = classify_confidence(0.99)
    assert r.tier == FixFirstTier.AUTO_FIX
    assert r.confidence == 99.0

    r2 = classify_confidence(0.5)
    assert r2.tier == FixFirstTier.INFORMATIONAL
    assert r2.confidence == 50.0


def test_missing_confidence_defaults_to_informational_not_auto_fix():
    """confidence가 없으면 안전 기본값(정보성)으로 처리 — 자동조치 금지."""
    r = classify_confidence(None)
    assert r.tier == FixFirstTier.INFORMATIONAL
    assert r.confidence is None


def test_unparseable_confidence_defaults_to_informational():
    for bad in ["not a number", [], {}, object()]:
        r = classify_confidence(bad)
        assert r.tier == FixFirstTier.INFORMATIONAL
        assert r.confidence is None


def test_out_of_range_confidence_is_unparseable():
    """음수나 100 초과는 유효하지 않은 값으로 처리 (오분류 방지)."""
    assert classify_confidence(-5).confidence is None
    assert classify_confidence(150).confidence is None


def test_bool_is_not_treated_as_numeric_confidence():
    """bool은 int의 서브클래스이므로 True==1, False==0으로 잘못 해석되지
    않도록 명시적으로 배제되어야 한다."""
    assert classify_confidence(True).confidence is None
    assert classify_confidence(False).confidence is None


def test_classify_findings_enriches_list_of_dicts():
    findings = [
        {"issue": "SQL injection", "confidence": 97},
        {"issue": "style nit", "confidence": 30},
        {"issue": "no confidence field"},
    ]
    result = classify_findings(findings)
    assert result[0]["fix_first_tier"] == "auto_fix"
    assert result[1]["fix_first_tier"] == "informational"
    assert result[2]["fix_first_tier"] == "informational"
    assert result[2]["fix_first_confidence"] is None
    # 원본 키(issue)는 보존되어야 함
    assert result[0]["issue"] == "SQL injection"


def test_classify_findings_does_not_mutate_input():
    findings = [{"issue": "x", "confidence": 96}]
    result = classify_findings(findings)
    assert "fix_first_tier" not in findings[0]  # 원본은 안 바뀜
    assert "fix_first_tier" in result[0]
