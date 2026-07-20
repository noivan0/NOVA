"""
nova.kernel.interrupt — Semantic Interrupt Scheduler (Phase 2)
==============================================================

takes 목록을 분석해 인터럽트 종류를 분류하고, 도메인별 전담 harness로
라우팅하는 코어 모듈.

설계 원칙:
  - 이 파일(코어)에는 HMG 전용 로직이 없음 — domain_routing.yaml로만 커스터마이즈
  - domain_routing.yaml만 교체하면 다른 조직에서도 즉시 사용 가능
  - Python 3.10+ 타입 힌트 사용
  - 모든 예외는 호출자가 폴백 처리할 수 있도록 명확히 전파

Phase 1 감사에서 발견된 spawn() 연결 문제(I-3)를 해결하는 구조를 제공.
brain_watcher._react()가 nova_events를 폴링해 실제 harness를 실행한다.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 기본 경로 ────────────────────────────────────────────────────────────────

_DEFAULT_ROUTING_YAML = Path(__file__).parent / "domain_routing.yaml"


# ── 인터럽트 종류 Enum ────────────────────────────────────────────────────────

class InterruptKind(str, Enum):
    """인터럽트 분류 종류.

    DOMAIN_RESEARCH : 특정 도메인 지식 부족 → 도메인 전담 harness 기동
    SELF_HEAL       : 버그/에러 패턴 누적 → investigate harness 기동
    GENERALIZE      : 같은 kind+holder 조합 3회 이상 → pattern 일반화
    SYNTHESIZE      : 대화 누적 (도메인 미매칭 폴백) → synthesize harness
    ALERT           : 모니터링 임계값 초과 (외부 시그널)
    """
    DOMAIN_RESEARCH = "domain_research"
    SELF_HEAL       = "self_heal"
    GENERALIZE      = "generalize"
    SYNTHESIZE      = "synthesize"
    ALERT           = "alert"


# ── Interrupt 데이터클래스 ────────────────────────────────────────────────────

@dataclasses.dataclass
class Interrupt:
    """분류된 인터럽트 하나를 나타내는 불변 데이터 구조.

    Attributes
    ----------
    kind:
        InterruptKind 중 하나.
    domain:
        식별된 도메인 슬러그. 예: ``"mms"``, ``"nuuseta"``, ``"system"``,
        ``"general"``.
    confidence:
        분류 신뢰도. 0.0 ~ 1.0.
    evidence:
        트리거된 takes의 claim 문자열 목록 (최대 5개).
    harness:
        실행할 harness 이름. domain_routing.yaml에서 매핑됨.
    priority:
        1 = 즉시 실행, 2 = 다음 기회, 3 = 배치.
    """
    kind:       InterruptKind
    domain:     str
    confidence: float
    evidence:   list[str]
    harness:    str
    priority:   int
    tier:       str = "warm"   # Phase 3: "hot"|"warm"|"cold" 메모리 계층 힌트


# ── InterruptRouter 클래스 ────────────────────────────────────────────────────

class InterruptRouter:
    """takes 목록을 분석해 인터럽트를 분류하고 harness로 라우팅하는 라우터.

    Parameters
    ----------
    routing_yaml:
        domain_routing.yaml 경로.  None이면 같은 디렉토리의 기본 파일 사용.
    """

    def __init__(self, routing_yaml: str | None = None) -> None:
        _yaml_path = Path(routing_yaml) if routing_yaml else _DEFAULT_ROUTING_YAML
        self._config: dict[str, Any] = self._load_yaml(_yaml_path)
        self._domains: dict[str, dict] = self._config.get("domains", {})
        self._defaults: dict[str, Any] = self._config.get("defaults", {})

    # ── 설정 로드 ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        """YAML 설정 파일 로드. 실패 시 빈 dict 반환 (폴백 안전)."""
        try:
            import yaml  # type: ignore[import]
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.warning("[interrupt] domain_routing.yaml 없음: %s — 도메인 분류 비활성화", path)
            return {}
        except Exception as e:
            logger.warning("[interrupt] YAML 로드 실패: %s — %s", path, e)
            return {}

    # ── classify() ───────────────────────────────────────────────────────────

    def classify(self, takes: list[dict], window: int = 50, tier: str = "warm") -> list[Interrupt]:
        """takes 목록을 분석해 우선순위 정렬된 Interrupt 목록을 반환.

        Parameters
        ----------
        takes:
            ``[{kind, claim, holder, recorded_at}, ...]`` 형식의 take 딕셔너리 목록.
        window:
            분석할 최신 takes 개수 (sliding window). 전체 스캔 방지 (I-1 수정).
            기본 50개. Phase 3에서 tier별로 다른 window 적용 가능.
        tier:
            Phase 3 메모리 계층 힌트. 현재 무시되나 인터페이스 예약 (I-3 수정).
            ``"hot"`` | ``"warm"`` | ``"cold"``

        Returns
        -------
        list[Interrupt]:
            우선순위 오름차순(1이 가장 높음)으로 정렬된 인터럽트 목록.
            중복 방지: 같은 (kind, domain) 조합은 1개만 반환.

        confidence 계산:
            DOMAIN_RESEARCH: matched / (min_matches * 2), 최대 1.0
            SELF_HEAL:       bug_count / 5, 최대 1.0
            GENERALIZE:      combo_count / 6, 최대 1.0
            SYNTHESIZE:      0.5 고정 (폴백)
        """
        if not takes:
            return []

        # I-1: sliding window — 최신 N개만 처리 (전체 스캔 방지)
        # [chain] 접두사 fact는 체인 메타데이터 — domain 분류에서 제외 (noise 억제)
        filtered = [t for t in takes if not t.get("claim", "").startswith("[chain]")]
        windowed = filtered[-window:] if len(filtered) > window else filtered
        # 필터 후 비어있으면 원본 사용
        if not windowed:
            windowed = takes[-window:] if len(takes) > window else takes

        results: list[Interrupt] = []

        # ── 1. SELF_HEAL: BUG/에러 패턴 3개 이상 ────────────────────────────
        bug_takes = [
            t for t in windowed
            if self._is_bug_take(t.get("claim", "") or t.get("content", ""))
        ]
        if len(bug_takes) >= 3:
            evidence = [t.get("claim", t.get("content", ""))[:80] for t in bug_takes[:5]]
            conf = min(1.0, len(bug_takes) / 5.0)
            harness = self._domain_harness("system") or self._defaults.get("fallback_harness", "research")
            results.append(Interrupt(
                kind=InterruptKind.SELF_HEAL,
                domain="system",
                confidence=conf,
                evidence=evidence,
                harness=harness,
                priority=1,
            ))
            logger.debug("[interrupt] SELF_HEAL: BUG 패턴 %d개 → %s", len(bug_takes), harness)

        # ── 2. DOMAIN_RESEARCH: 도메인 키워드 매칭 ───────────────────────────
        domain_hits = self._match_domains(windowed)
        for domain_slug, matched_takes in domain_hits.items():
            domain_cfg = self._domains.get(domain_slug, {})
            min_matches = domain_cfg.get("min_matches", 2)
            if len(matched_takes) < min_matches:
                continue
            evidence = [t.get("claim", t.get("content", ""))[:80] for t in matched_takes[:5]]
            conf = min(1.0, len(matched_takes) / (min_matches * 2))
            harness = domain_cfg.get("harness") or self._defaults.get("fallback_harness", "research")
            results.append(Interrupt(
                kind=InterruptKind.DOMAIN_RESEARCH,
                domain=domain_slug,
                confidence=conf,
                evidence=evidence,
                harness=harness,
                priority=2,
            ))
            logger.debug(
                "[interrupt] DOMAIN_RESEARCH: %s 키워드 %d개 → %s",
                domain_slug, len(matched_takes), harness,
            )

        # ── 3. GENERALIZE: 같은 kind+holder 조합 3회 이상 ───────────────────
        combo_counts: dict[str, list[dict]] = {}
        for t in windowed:
            key = f"{t.get('kind', '')}::{t.get('holder', '')}"
            combo_counts.setdefault(key, []).append(t)
        for key, group in combo_counts.items():
            if len(group) >= 3:
                evidence = [t.get("claim", t.get("content", ""))[:80] for t in group[:5]]
                conf = min(1.0, len(group) / 6.0)
                harness = self._defaults.get("fallback_harness", "research")
                kind_str, holder_str = key.split("::", 1)
                results.append(Interrupt(
                    kind=InterruptKind.GENERALIZE,
                    domain=holder_str or "general",
                    confidence=conf,
                    evidence=evidence,
                    harness=harness,
                    priority=3,
                ))
                logger.debug("[interrupt] GENERALIZE: %s 조합 %d개", key, len(group))
                break  # 첫 번째만 (과잉 트리거 방지)

        # ── 4. SYNTHESIZE: 도메인 미매칭 폴백 (windowed takes N개 이상) ─────
        if not results:
            fallback_min = self._defaults.get("fallback_min_takes", 20)
            if len(windowed) >= fallback_min:
                harness = self._defaults.get("fallback_harness", "research")
                results.append(Interrupt(
                    kind=InterruptKind.SYNTHESIZE,
                    domain="general",
                    confidence=0.5,
                    evidence=[t.get("claim", t.get("content", ""))[:80] for t in takes[:3]],
                    harness=harness,
                    priority=3,
                ))
                logger.debug("[interrupt] SYNTHESIZE 폴백: takes %d개 → %s", len(takes), harness)

        # 중복 제거: 같은 (kind, domain) 조합은 신뢰도 높은 것 유지
        seen: set[tuple[str, str]] = set()
        deduped: list[Interrupt] = []
        for intr in sorted(results, key=lambda x: (x.priority, -x.confidence)):
            key_dedup = (intr.kind.value, intr.domain)
            if key_dedup not in seen:
                seen.add(key_dedup)
                deduped.append(intr)

        return sorted(deduped, key=lambda x: (x.priority, -x.confidence))

    # ── route() ──────────────────────────────────────────────────────────────

    def route(self, interrupt: Interrupt) -> str:
        """인터럽트를 분석해 실행할 harness 이름을 반환.

        Parameters
        ----------
        interrupt:
            classify()가 반환한 Interrupt 객체.

        Returns
        -------
        str:
            harness 이름 (예: ``"research"``, ``"investigate"``).
        """
        # domain_routing.yaml 설정 우선, 없으면 interrupt.harness 사용
        domain_cfg = self._domains.get(interrupt.domain, {})
        harness = domain_cfg.get("harness") or interrupt.harness
        return harness or self._defaults.get("fallback_harness", "research")

    # ── should_trigger() ─────────────────────────────────────────────────────

    def should_trigger(self, interrupt: Interrupt, last_triggered: dict[str, float]) -> bool:
        """쿨다운/중복 방지 체크.

        Parameters
        ----------
        interrupt:
            체크할 인터럽트.
        last_triggered:
            ``{harness_name: epoch_timestamp}`` 형식의 최근 실행 기록.

        Returns
        -------
        bool:
            True면 트리거 허용, False면 쿨다운 중.
        """
        harness = interrupt.harness
        last_ts = last_triggered.get(harness, 0.0)
        now = time.time()

        # domain_routing.yaml에서 쿨다운 읽기
        domain_cfg = self._domains.get(interrupt.domain, {})
        cooldown_min = domain_cfg.get(
            "cooldown_min",
            self._defaults.get("fallback_cooldown_min", 20),
        )
        cooldown_s = cooldown_min * 60

        elapsed = now - last_ts
        if elapsed < cooldown_s:
            logger.debug(
                "[interrupt] 쿨다운 중: %s (남은 %.0fs / %dmin)",
                harness, cooldown_s - elapsed, cooldown_min,
            )
            return False
        return True

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_bug_take(claim: str) -> bool:
        """take의 claim이 BUG/에러 관련인지 판별."""
        import re
        _BUG_RE = re.compile(
            r"BUG|버그|에러|Error|FAIL|수정 필요|traceback|exception|실패|오류",
            re.IGNORECASE | re.UNICODE,
        )
        return bool(_BUG_RE.search(claim))

    def _domain_harness(self, domain_slug: str) -> str | None:
        """도메인 설정에서 harness 이름 반환. 없으면 None."""
        return self._domains.get(domain_slug, {}).get("harness")

    def _match_domains(self, takes: list[dict]) -> dict[str, list[dict]]:
        """모든 도메인 키워드에 대해 매칭된 takes를 도메인별로 묶어 반환.

        Returns
        -------
        dict[str, list[dict]]:
            ``{domain_slug: [matched_take, ...]}``
        """
        result: dict[str, list[dict]] = {}
        for slug, cfg in self._domains.items():
            keyword_groups: list[list[str]] = cfg.get("keywords", [])
            # keywords가 list[list[str]] 또는 list[str] 모두 지원
            flat_keywords: list[str] = []
            for group in keyword_groups:
                if isinstance(group, list):
                    flat_keywords.extend(group)
                elif isinstance(group, str):
                    flat_keywords.append(group)

            if not flat_keywords:
                continue

            matched: list[dict] = []
            for take in takes:
                claim = take.get("claim", take.get("content", "")) or ""
                if any(kw.lower() in claim.lower() for kw in flat_keywords):
                    matched.append(take)
            if matched:
                result[slug] = matched
        return result
