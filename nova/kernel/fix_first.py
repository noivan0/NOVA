"""
nova.kernel.fix_first — Fix-First Heuristic (gstack parity)
==============================================================

gstack의 "Fix-First Heuristic: 95%+ confidence → AUTO-FIX / flag as critical,
below 85% → ASK / informational" 원칙을 결정론적 코드로 재현한다.

기존 nova_codex_gate.py는 이 문구를 LLM 프롬프트 안에 "이렇게 판단해줘"라고
지시만 했을 뿐, LLM이 실제로 그 규칙을 따랐는지 검증하거나 강제하는 코드가
없었다 — confidence 필드 자체가 응답 스키마에 없어서 파싱할 수도 없었다.

이 모듈은:
  1. LLM 응답에서 confidence 값(0~100 또는 0.0~1.0)을 강건하게 파싱
  2. 파싱된 값을 기준으로 AUTO_FIX / CRITICAL / INFORMATIONAL 세 단계로
     **코드가** 분류 (LLM의 자체 판단에 의존하지 않음)
  3. LLM이 confidence를 아예 안 줬거나 파싱 불가능하면 안전 기본값
     (INFORMATIONAL — 가장 보수적, 자동 조치 안 함)으로 처리
"""

from __future__ import annotations

import dataclasses
import re
from enum import Enum
from typing import Any


class FixFirstTier(str, Enum):
    """Fix-First Heuristic 분류 결과."""
    AUTO_FIX = "auto_fix"            # confidence >= 95 — 자동수정 가능 수준
    CRITICAL = "critical"            # 85 <= confidence < 95 — critical로 플래그, 사람 확인 필요
    INFORMATIONAL = "informational"  # confidence < 85 (또는 파싱 불가) — 정보 제공만


@dataclasses.dataclass
class FixFirstClassification:
    tier: FixFirstTier
    confidence: float | None   # 0~100 스케일로 정규화된 값, 파싱 실패 시 None
    raw_value: Any              # 원본 값 (디버깅용)


_AUTO_FIX_THRESHOLD = 95.0
_CRITICAL_THRESHOLD = 85.0


def _normalize_confidence(value: Any) -> float | None:
    """다양한 표현(95, 95.0, "95%", "0.95", 0.95)을 0~100 스케일로 정규화.

    파싱 불가능하면 None.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool은 int의 서브클래스라 먼저 배제
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # 0.0~1.0 범위면 비율로 간주해 100배, 아니면 이미 percentage로 간주
        if 0.0 <= v <= 1.0:
            return v * 100.0
        if 0.0 <= v <= 100.0:
            return v
        return None
    if isinstance(value, str):
        s = value.strip().rstrip("%")
        m = re.match(r"^-?\d+(\.\d+)?$", s)
        if not m:
            return None
        return _normalize_confidence(float(s))
    return None


def classify_confidence(confidence: Any) -> FixFirstClassification:
    """confidence 값을 Fix-First 3단계로 분류.

    파싱 불가/누락 시 가장 보수적인 INFORMATIONAL로 처리 (자동조치 없음 —
    "확신 없으면 아무것도 자동으로 하지 않는다"는 안전 기본값).
    """
    norm = _normalize_confidence(confidence)
    if norm is None:
        return FixFirstClassification(tier=FixFirstTier.INFORMATIONAL, confidence=None, raw_value=confidence)
    if norm >= _AUTO_FIX_THRESHOLD:
        tier = FixFirstTier.AUTO_FIX
    elif norm >= _CRITICAL_THRESHOLD:
        tier = FixFirstTier.CRITICAL
    else:
        tier = FixFirstTier.INFORMATIONAL
    return FixFirstClassification(tier=tier, confidence=norm, raw_value=confidence)


def classify_findings(findings: list[dict]) -> list[dict]:
    """findings 목록(각각 {"confidence": ..., ...} 형태)에 fix_first_tier를
    실제로 부여해 반환. LLM이 각 finding에 confidence를 함께 보고하도록
    스키마를 확장했을 때 사용 (gpt_audit / claude_review 응답 후처리용).

    finding에 confidence 키가 없으면 tier=informational로 부여된다.
    """
    result = []
    for f in findings:
        classification = classify_confidence(f.get("confidence"))
        enriched = dict(f)
        enriched["fix_first_tier"] = classification.tier.value
        enriched["fix_first_confidence"] = classification.confidence
        result.append(enriched)
    return result
