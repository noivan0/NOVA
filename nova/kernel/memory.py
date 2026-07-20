"""
nova.kernel.memory — NOVA Memory Hierarchy Layer (Phase 3)
===========================================================

brain.db의 takes/pages를 hot/warm/cold 계층으로 구분하여 접근하는
시간 인덱스 레이어. HMG 전용 로직 없이 순수 시간 경계 기반으로 동작.

계층 정의:
  hot  : created_at > now - hot_hours  (기본 1시간)
  warm : created_at > now - warm_hours (기본 168시간 = 7일), hot 포함
  cold : warm_hours 이전 모든 데이터

환경변수 오버라이드:
  NOVA_HOT_HOURS   — hot 경계 (float, 시간 단위)
  NOVA_WARM_HOURS  — warm 경계 (float, 시간 단위)

사용 예:
    from nova.kernel.memory import MemoryLayer, TierConfig
    layer = MemoryLayer("~/.nova/brain.db")
    hot_takes = layer.get_takes(tier="hot")
    summary   = layer.summarize()
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# ── 타입 별칭 ─────────────────────────────────────────────────────────────────

Tier = Literal["hot", "warm", "cold"]
TierOrAuto = Literal["hot", "warm", "cold", "auto"]


# ── TierConfig ────────────────────────────────────────────────────────────────

@dataclass
class TierConfig:
    """hot/warm/cold 계층 시간 경계 설정.

    Attributes
    ----------
    hot_hours:
        hot 계층 상한 (시간). 현재 시각 기준 이 시간 이내의 데이터가 hot.
        기본 1.0시간.
    warm_hours:
        warm 계층 상한 (시간). 현재 시각 기준 이 시간 이내의 데이터가 warm(hot 포함).
        기본 168.0시간 (7일).
    """
    hot_hours:  float = 1.0
    warm_hours: float = 168.0   # 7일 = 168시간

    # hot_hours < warm_hours 보장
    def __post_init__(self) -> None:
        if self.hot_hours <= 0:
            raise ValueError(f"hot_hours는 양수여야 합니다: {self.hot_hours}")
        if self.warm_hours <= self.hot_hours:
            raise ValueError(
                f"warm_hours({self.warm_hours}) > hot_hours({self.hot_hours}) 이어야 합니다."
            )

    @classmethod
    def from_env(cls) -> "TierConfig":
        """환경변수(NOVA_HOT_HOURS, NOVA_WARM_HOURS)로 TierConfig 생성.

        환경변수가 없거나 파싱 실패 시 기본값 사용.
        """
        try:
            hot  = float(os.environ["NOVA_HOT_HOURS"])
        except (KeyError, ValueError):
            hot  = 1.0
        try:
            warm = float(os.environ["NOVA_WARM_HOURS"])
        except (KeyError, ValueError):
            warm = 168.0
        return cls(hot_hours=hot, warm_hours=warm)


# ── MemoryLayer ───────────────────────────────────────────────────────────────

class MemoryLayer:
    """brain.db takes/pages에 계층별 접근 레이어.

    세션 내 단기 처리 동작(hot),
    현재 컨텍스트(warm), 장기 지식(cold)으로 구분.

    읽기전용 쿼리만 수행 (INSERT/UPDATE 없음 → WAL 충돌 방지).

    Parameters
    ----------
    brain_db:
        brain.db 경로 (절대 경로 또는 ~/... 형태 모두 허용).
    config:
        TierConfig 인스턴스. None이면 환경변수 기반 자동 생성.
    """

    def __init__(
        self,
        brain_db: str,
        config: Optional[TierConfig] = None,
    ) -> None:
        self._db_path = str(Path(brain_db).expanduser().resolve())
        self.config   = config if config is not None else TierConfig.from_env()

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """brain.db 읽기 전용 연결 (WAL, busy_timeout 10초)."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA query_only=ON")  # 실수 쓰기 방지
        return conn

    def _now_utc(self) -> datetime:
        """현재 UTC datetime 반환."""
        return datetime.now(timezone.utc)

    def _hot_cutoff(self) -> str:
        """hot 경계 ISO8601 문자열 (UTC)."""
        dt = self._now_utc() - timedelta(hours=self.config.hot_hours)
        return dt.isoformat()

    def _warm_cutoff(self) -> str:
        """warm 경계 ISO8601 문자열 (UTC)."""
        dt = self._now_utc() - timedelta(hours=self.config.warm_hours)
        return dt.isoformat()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """sqlite3.Row → dict 변환."""
        return dict(row)

    # ── tier_of() ────────────────────────────────────────────────────────────

    def tier_of(self, created_at: str) -> Tier:
        """created_at ISO8601 문자열로 tier를 판별하여 반환.

        Parameters
        ----------
        created_at:
            ISO8601 형식 타임스탬프 (예: ``"2026-07-16T10:00:00"`` 또는
            ``"2026-07-16T10:00:00+00:00"``).

        Returns
        -------
        Tier:
            ``"hot"`` | ``"warm"`` | ``"cold"``

        Notes
        -----
        timezone 정보가 없는 naive datetime은 UTC로 간주.
        """
        # 파싱 — +00:00 또는 Z 처리
        ts = created_at.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            # 파싱 실패 시 cold 폴백
            return "cold"

        # naive → UTC 부착
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        now   = self._now_utc()
        delta = now - dt

        if delta <= timedelta(hours=self.config.hot_hours):
            return "hot"
        if delta <= timedelta(hours=self.config.warm_hours):
            return "warm"
        return "cold"

    # ── tier_bounds() ─────────────────────────────────────────────────────────

    def tier_bounds(self, tier: Tier) -> dict[str, Optional[str]]:
        """tier에 해당하는 after/before 시간 경계를 반환.

        kb_read()의 after/before 파라미터와 직접 연결됨.

        Parameters
        ----------
        tier:
            ``"hot"`` | ``"warm"`` | ``"cold"``

        Returns
        -------
        dict:
            ``{"after": ISO8601 | None, "before": ISO8601 | None}``

            - hot  : after=now-1h,   before=None
            - warm : after=now-168h, before=None  (hot 포함)
            - cold : after=None,     before=now-168h
        """
        if tier == "hot":
            return {"after": self._hot_cutoff(), "before": None}
        elif tier == "warm":
            return {"after": self._warm_cutoff(), "before": None}
        elif tier == "cold":
            return {"after": None, "before": self._warm_cutoff()}
        else:
            raise ValueError(f"알 수 없는 tier: {tier!r}. 'hot'|'warm'|'cold' 중 하나.")

    # ── get_takes() ───────────────────────────────────────────────────────────

    def get_takes(
        self,
        tier: TierOrAuto = "auto",
        kind: Optional[str] = None,
        limit: int = 20,
        agent: Optional[str] = None,
    ) -> list[dict]:
        """계층별 takes 조회.

        Parameters
        ----------
        tier:
            ``"hot"`` | ``"warm"`` | ``"cold"`` | ``"auto"``

            - ``"hot"``  : created_at > now - hot_hours
            - ``"warm"`` : created_at > now - warm_hours (hot 포함)
            - ``"cold"`` : warm_hours 이전 데이터
            - ``"auto"`` : hot 먼저, 부족 시 warm에서 보충 (폭포 확장)
        kind:
            ``"fact"`` | ``"insight"`` | ``"lesson"`` | ``"pattern"`` | None.
            None이면 전체 kind 대상.
        limit:
            최대 반환 수 (기본 20).
        agent:
            특정 에이전트의 takes만 조회. None이면 전체 에이전트.

        Returns
        -------
        list[dict]:
            takes 행의 dict 목록. 각 dict는 takes 테이블 컬럼을 그대로 포함.
        """
        if tier == "auto":
            return self._get_takes_auto(kind=kind, limit=limit, agent=agent)

        bounds = self.tier_bounds(tier)  # type: ignore[arg-type]
        return self._query_takes(
            after=bounds["after"],
            before=bounds["before"],
            kind=kind,
            limit=limit,
            agent=agent,
            exclude_chain=(tier == "cold"),   # C-2: cold는 [chain] 노이즈 자동 제외
        )

    def _get_takes_auto(
        self,
        kind: Optional[str],
        limit: int,
        agent: Optional[str],
    ) -> list[dict]:
        """auto 모드: hot 먼저 조회, 부족 시 warm에서 추가 보충 (폭포 확장)."""
        hot_results = self._query_takes(
            after=self._hot_cutoff(),
            before=None,
            kind=kind,
            limit=limit,
            agent=agent,
        )
        if len(hot_results) >= limit:
            return hot_results[:limit]

        # hot이 부족 → warm에서 보충 (이미 hot을 warm이 포함하므로 warm 전체 재조회 후 슬라이스)
        remaining = limit - len(hot_results)
        warm_results = self._query_takes(
            after=self._warm_cutoff(),
            before=self._hot_cutoff(),   # hot 구간 제외 (중복 방지)
            kind=kind,
            limit=remaining,
            agent=agent,
        )
        # C-6 수정: warm(오래된) + hot(최신) 순서 — classify()의 takes[-window:] 방향 맞춤
        return warm_results + hot_results

    def _query_takes(
        self,
        after: Optional[str],
        before: Optional[str],
        kind: Optional[str],
        limit: int,
        agent: Optional[str],
        exclude_chain: bool = False,    # C-2: cold tier [chain] 노이즈 제외
    ) -> list[dict]:
        """실제 takes SQL 쿼리 실행.

        주의: brain.db의 created_at이 KST(+09:00) 등 타임존 포함 문자열로
        저장될 수 있음. SQLite datetime() 함수로 양쪽 모두 UTC 정규화 후 비교.

        Raises
        ------
        RuntimeError
            스키마 불일치 (created_at 컬럼 없음 등). 호출자가 처리해야 함.
        """
        import sqlite3 as _sq
        conditions: list[str] = []
        params: list[Any]     = []

        if after is not None:
            conditions.append("datetime(created_at) > datetime(?)")
            params.append(after)
        if before is not None:
            conditions.append("datetime(created_at) <= datetime(?)")
            params.append(before)
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if agent is not None:
            conditions.append("holder = ?")
            params.append(agent)
        if exclude_chain:
            # C-2: cold의 [chain] 노이즈(60%) 자동 제외
            conditions.append("claim NOT LIKE '[chain]%'")

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        sql = f"""
            SELECT id, page_id, kind, holder, claim, weight,
                   created_at, updated_at, superseded_by, source,
                   confidence, evidence, outcome, brier_score
            FROM takes
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """

        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except _sq.OperationalError as e:
            if "no such column" in str(e):
                raise RuntimeError(
                    f"[MemoryLayer] 스키마 불일치 — DB 마이그레이션 필요: {e}"
                ) from e
            logger.warning("[MemoryLayer] takes 조회 실패, 빈 리스트 반환: %s", e)
            return []

        return [self._row_to_dict(r) for r in rows]

    # ── get_pages() ───────────────────────────────────────────────────────────

    def get_pages(
        self,
        tier: TierOrAuto = "auto",
        limit: int = 10,
        agent: Optional[str] = None,
    ) -> list[dict]:
        """계층별 pages 조회 (updated_at 기준).

        Parameters
        ----------
        tier:
            ``"hot"`` | ``"warm"`` | ``"cold"`` | ``"auto"``
        limit:
            최대 반환 수 (기본 10).
        agent:
            특정 에이전트의 pages만 조회. None이면 전체.

        Returns
        -------
        list[dict]:
            pages 행의 dict 목록.
        """
        if tier == "auto":
            # pages는 updated_at 기반 — hot 먼저, 부족 시 warm 보충
            hot_p = self._query_pages(
                after=self._hot_cutoff(),
                before=None,
                limit=limit,
                agent=agent,
            )
            if len(hot_p) >= limit:
                return hot_p[:limit]
            remaining = limit - len(hot_p)
            warm_p = self._query_pages(
                after=self._warm_cutoff(),
                before=self._hot_cutoff(),
                limit=remaining,
                agent=agent,
            )
            return hot_p + warm_p

        bounds = self.tier_bounds(tier)  # type: ignore[arg-type]
        return self._query_pages(
            after=bounds["after"],
            before=bounds["before"],
            limit=limit,
            agent=agent,
        )

    def _query_pages(
        self,
        after: Optional[str],
        before: Optional[str],
        limit: int,
        agent: Optional[str],
    ) -> list[dict]:
        """실제 pages SQL 쿼리 실행 (updated_at 기준).

        KST(+09:00) 등 타임존 포함 문자열 대응:
        SQLite datetime() 함수로 UTC 정규화 후 비교.
        """
        conditions: list[str] = []
        params: list[Any]     = []

        if after is not None:
            conditions.append("datetime(updated_at) > datetime(?)")
            params.append(after)
        if before is not None:
            conditions.append("datetime(updated_at) <= datetime(?)")
            params.append(before)
        if agent is not None:
            conditions.append("agent = ?")
            params.append(agent)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        sql = f"""
            SELECT id, path, title, page_type, agent,
                   char_count, health_score,
                   created_at, updated_at
            FROM pages
            {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [self._row_to_dict(r) for r in rows]

    # ── summarize() ──────────────────────────────────────────────────────────

    def summarize(self, tier: TierOrAuto = "auto") -> dict[str, dict[str, int]]:
        """hot/warm/cold 별 통계 요약 반환.

        Parameters
        ----------
        tier:
            ``"auto"``이면 모든 계층 통계 반환 (기본).
            특정 tier 지정 시 해당 계층만 포함.

        Returns
        -------
        dict:
            ``{
                "hot":  {"takes": N, "pages": N},
                "warm": {"takes": N, "pages": N},
                "cold": {"takes": N, "pages": N},
            }``
            tier 지정 시 해당 키만 포함.
        """
        hot_cut  = self._hot_cutoff()
        warm_cut = self._warm_cutoff()

        with self._connect() as conn:
            # takes 카운트
            hot_takes = conn.execute(
                "SELECT COUNT(*) FROM takes WHERE datetime(created_at) > datetime(?)", (hot_cut,)
            ).fetchone()[0]
            warm_takes_only = conn.execute(
                "SELECT COUNT(*) FROM takes WHERE datetime(created_at) > datetime(?) AND datetime(created_at) <= datetime(?)",
                (warm_cut, hot_cut),
            ).fetchone()[0]
            cold_takes = conn.execute(
                "SELECT COUNT(*) FROM takes WHERE datetime(created_at) <= datetime(?)", (warm_cut,)
            ).fetchone()[0]

            # pages 카운트 (updated_at 기준)
            hot_pages = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE datetime(updated_at) > datetime(?)", (hot_cut,)
            ).fetchone()[0]
            warm_pages_only = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE datetime(updated_at) > datetime(?) AND datetime(updated_at) <= datetime(?)",
                (warm_cut, hot_cut),
            ).fetchone()[0]
            cold_pages = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE datetime(updated_at) <= datetime(?)", (warm_cut,)
            ).fetchone()[0]

        all_stats: dict[str, dict[str, int]] = {
            "hot":  {"takes": hot_takes,       "pages": hot_pages},
            "warm": {"takes": warm_takes_only,  "pages": warm_pages_only},
            "cold": {"takes": cold_takes,       "pages": cold_pages},
        }

        if tier == "auto":
            return all_stats
        return {tier: all_stats[tier]}  # type: ignore[index]


# ── 편의 팩토리 ───────────────────────────────────────────────────────────────

def make_layer(brain_db: Optional[str] = None) -> MemoryLayer:
    """기본 설정으로 MemoryLayer 인스턴스 생성.

    brain_db를 생략하면 NOVA_HOME 환경변수 또는 ~/.nova/brain.db 사용.
    """
    if brain_db is None:
        nova_home_str = (os.environ.get("NOVA_HOME") or "~/.nova").strip() or "~/.nova"
        nova_home = Path(nova_home_str).expanduser().resolve()
        brain_db  = str(nova_home / "brain.db")
    return MemoryLayer(brain_db=brain_db)
