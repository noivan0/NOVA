"""
tests/unit/test_memory.py -- MemoryLayer Phase 3 단위 테스트

실제 brain.db (~/.nova/brain.db)를 사용하는 통합 단위 테스트.
brain.db가 없는 환경에서는 임시 DB를 생성하여 검증.

테스트 목록:
  1. tier_of() -- ISO8601 입력에 대한 정확한 tier 반환
  2. get_takes(tier="hot") -- 실제 brain.db 시간 필터 쿼리
  3. tier_bounds() -- after/before 경계 정확성
  4. summarize() -- hot/warm/cold 총계 정합성 (total == DB 전체 수)
  5. kb_read(tier="hot") -- syscall.py 연동 시간 필터 확인
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

# -- 테스트 대상 임포트 -------------------------------------------------------

from nova.kernel.memory import MemoryLayer, TierConfig, make_layer
from nova.kernel.syscall import KernelAPI


# -- 픽스처 ------------------------------------------------------------------

REAL_BRAIN_DB = Path.home() / ".nova" / "brain.db"


def _make_temp_db() -> str:
    """테스트용 최소 임시 brain.db 생성. 실제 스키마 그대로."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="nova_test_")
    os.close(fd)

    now_utc = datetime.now(timezone.utc)
    ts_hot   = (now_utc - timedelta(minutes=30)).isoformat()
    ts_warm  = (now_utc - timedelta(hours=48)).isoformat()
    ts_cold  = (now_utc - timedelta(hours=200)).isoformat()

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE takes (
            id TEXT PRIMARY KEY,
            page_id TEXT,
            kind TEXT DEFAULT 'fact',
            holder TEXT,
            claim TEXT NOT NULL,
            weight REAL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_by TEXT,
            source TEXT,
            confidence REAL,
            evidence TEXT,
            outcome TEXT,
            brier_score REAL
        );
        CREATE TABLE pages (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            title TEXT,
            page_type TEXT DEFAULT 'general',
            agent TEXT,
            tags TEXT,
            compiled_truth TEXT DEFAULT '',
            timeline TEXT DEFAULT '',
            char_count INTEGER DEFAULT 0,
            content_hash TEXT,
            indexed_at TEXT,
            health_score REAL DEFAULT 1.0,
            embedding_id TEXT,
            summary TEXT,
            metadata_json TEXT,
            section TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            emotional_weight REAL DEFAULT 0.5,
            has_contradictions INTEGER DEFAULT 0
        );
    """)
    conn.executemany(
        "INSERT INTO takes (id, kind, holder, claim, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("t-hot-1",  "fact",    "nova-test", "hot claim 1",  ts_hot,  ts_hot),
            ("t-hot-2",  "insight", "nova-test", "hot claim 2",  ts_hot,  ts_hot),
            ("t-warm-1", "lesson",  "nova-test", "warm claim 1", ts_warm, ts_warm),
            ("t-warm-2", "pattern", "nova-test", "warm claim 2", ts_warm, ts_warm),
            ("t-cold-1", "fact",    "nova-test", "cold claim 1", ts_cold, ts_cold),
            ("t-cold-2", "fact",    "nova-test", "cold claim 2", ts_cold, ts_cold),
        ],
    )
    conn.executemany(
        "INSERT INTO pages (id, path, title, agent, compiled_truth, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("p-hot",  "workspace/hot_page.md",  "Hot Page",  "nova-test", "hot content",  ts_hot),
            ("p-warm", "workspace/warm_page.md", "Warm Page", "nova-test", "warm content", ts_warm),
            ("p-cold", "workspace/cold_page.md", "Cold Page", "nova-test", "cold content", ts_cold),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def temp_db() -> Generator[str, None, None]:
    """모듈 범위 임시 brain.db 픽스처."""
    path = _make_temp_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def layer(temp_db: str) -> MemoryLayer:
    """임시 DB 기반 MemoryLayer 픽스처."""
    return MemoryLayer(brain_db=temp_db)


@pytest.fixture(scope="module")
def real_layer() -> Optional[MemoryLayer]:
    """실제 brain.db 픽스처. 없으면 None 반환."""
    if REAL_BRAIN_DB.exists():
        return MemoryLayer(brain_db=str(REAL_BRAIN_DB))
    return None


# -- 테스트 1: tier_of() -----------------------------------------------------

class TestTierOf:
    """tier_of() 메서드 -- ISO8601 to tier 변환 정확성."""

    def test_hot_tier(self, layer: MemoryLayer) -> None:
        """30분 전 타임스탬프 -> hot 반환."""
        ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        result = layer.tier_of(ts)
        assert result == "hot", f"기대 'hot', 실제 '{result}' (ts={ts})"

    def test_warm_tier(self, layer: MemoryLayer) -> None:
        """48시간 전 타임스탬프 -> warm 반환."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        result = layer.tier_of(ts)
        assert result == "warm", f"기대 'warm', 실제 '{result}' (ts={ts})"

    def test_cold_tier(self, layer: MemoryLayer) -> None:
        """200시간 전 타임스탬프 -> cold 반환."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        result = layer.tier_of(ts)
        assert result == "cold", f"기대 'cold', 실제 '{result}' (ts={ts})"

    def test_exact_boundary_hot(self, layer: MemoryLayer) -> None:
        """hot_hours 경계 직후 -> warm 반환 (경계 초과)."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=1, seconds=1)).isoformat()
        result = layer.tier_of(ts)
        assert result == "warm", f"hot 경계 초과 -> 'warm' 기대, 실제 '{result}'"

    def test_naive_datetime_as_utc(self, layer: MemoryLayer) -> None:
        """timezone 없는 naive datetime -> UTC로 간주."""
        ts_naive = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        result = layer.tier_of(ts_naive)
        assert result == "hot", f"naive datetime UTC 간주 -> 'hot' 기대, 실제 '{result}'"

    def test_invalid_timestamp_returns_cold(self, layer: MemoryLayer) -> None:
        """파싱 불가 타임스탬프 -> cold 폴백."""
        result = layer.tier_of("not-a-timestamp")
        assert result == "cold", f"파싱 실패 -> 'cold' 기대, 실제 '{result}'"

    def test_specific_timestamp_2026(self, layer: MemoryLayer) -> None:
        """'2026-07-16T10:00:00' -- 현재 기준 tier 검증 (완료 기준 #7)."""
        ts = "2026-07-16T10:00:00"
        result = layer.tier_of(ts)
        dt_ts  = datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)
        now    = datetime.now(timezone.utc)
        delta  = now - dt_ts
        hot_h  = layer.config.hot_hours
        warm_h = layer.config.warm_hours
        if delta <= timedelta(hours=hot_h):
            expected = "hot"
        elif delta <= timedelta(hours=warm_h):
            expected = "warm"
        else:
            expected = "cold"
        assert result == expected, (
            f"tier_of('2026-07-16T10:00:00') -> 기대 '{expected}', 실제 '{result}'"
        )


# -- 테스트 2: get_takes(tier="hot") -----------------------------------------

class TestGetTakes:
    """get_takes() -- 계층별 takes 쿼리."""

    def test_hot_takes_count(self, layer: MemoryLayer) -> None:
        """hot tier -> 2개 반환 (임시 DB 기준)."""
        results = layer.get_takes(tier="hot")
        assert len(results) == 2, f"hot takes 2개 기대, 실제 {len(results)}개"

    def test_hot_takes_all_in_hot_range(self, layer: MemoryLayer) -> None:
        """hot tier 결과의 created_at이 모두 hot 경계 내에 있음."""
        results = layer.get_takes(tier="hot")
        for r in results:
            t = layer.tier_of(r["created_at"])
            assert t == "hot", (
                f"hot 쿼리 결과에 non-hot 레코드 포함: created_at={r['created_at']}, tier={t}"
            )

    def test_warm_takes_includes_hot(self, layer: MemoryLayer) -> None:
        """warm tier -> hot(2) + warm_only(2) = 4개 반환."""
        results = layer.get_takes(tier="warm")
        assert len(results) == 4, f"warm takes 4개 기대, 실제 {len(results)}개"

    def test_cold_takes_count(self, layer: MemoryLayer) -> None:
        """cold tier -> 2개 반환."""
        results = layer.get_takes(tier="cold")
        assert len(results) == 2, f"cold takes 2개 기대, 실제 {len(results)}개"

    def test_auto_waterfall_expands(self, layer: MemoryLayer) -> None:
        """auto 모드 -- limit가 hot보다 크면 warm에서 보충."""
        results = layer.get_takes(tier="auto", limit=10)
        assert len(results) == 4, (
            f"auto 폭포 확장 -> 4개 기대 (hot 2 + warm_only 2), 실제 {len(results)}개"
        )

    def test_kind_filter(self, layer: MemoryLayer) -> None:
        """kind='fact' 필터 -- fact 타입만 반환."""
        results = layer.get_takes(tier="warm", kind="fact")
        kinds = {r["kind"] for r in results}
        assert kinds == {"fact"}, f"kind='fact' 필터 실패, 반환 kind 집합: {kinds}"

    def test_limit_respected(self, layer: MemoryLayer) -> None:
        """limit 파라미터 준수."""
        results = layer.get_takes(tier="cold", limit=1)
        assert len(results) <= 1, f"limit=1 초과 반환: {len(results)}개"

    def test_real_db_hot_takes(self, real_layer: Optional[MemoryLayer]) -> None:
        """실제 brain.db에서 get_takes(tier='hot') 쿼리 실행 (완료 기준 #5).

        P1 fix (2026-08-18): REAL_BRAIN_DB(~/.nova/brain.db)는 실행 환경마다
        완전히 다른 실제 운영 DB를 가리킬 수 있는 개인 환경 종속 경로다.
        이 저장소를 오픈소스로 사용하는 사람의 머신에 우연히 그 경로의
        brain.db가 있을 경우 그 DB의 takes 스키마가 nova.kernel.memory가
        기대하는 컬럼 구성(confidence 등)과 다를 수 있고, 이는 nova 자체의
        결함이 아니라 그 브레인 DB가 다른 버전/다른 목적으로 만들어졌다는
        뜻이다. 원래 코드는 이 RuntimeError를 잡지 않아 "실제 brain.db가
        존재하지만 스키마가 다른" 흔한 상황에서 테스트가 실패했다.
        스키마 불일치는 skip 처리하고, 파일이 아예 없을 때와 동일하게
        취급한다 — 이 테스트는 오직 "temp_db 픽스처로 만든 표준 스키마
        DB에서 get_takes가 정상 동작하는가"를 이미 다른 테스트로 커버하며,
        이 테스트는 그 위에 실제 파일 I/O 경로까지 도는지 추가로 확인하는
        선택적(best-effort) 스모크 테스트일 뿐이다.
        """
        if real_layer is None:
            pytest.skip("실제 brain.db 없음 -- 건너뜀")
        try:
            results = real_layer.get_takes(tier="hot")
        except RuntimeError as exc:
            pytest.skip(
                f"실제 brain.db 스키마가 이 nova 버전과 다름(개인 환경 종속) -- 건너뜀: {exc}"
            )
        assert isinstance(results, list), "결과가 list 아님"
        for r in results:
            assert "id" in r,         "결과 dict에 'id' 없음"
            assert "claim" in r,      "결과 dict에 'claim' 없음"
            assert "created_at" in r, "결과 dict에 'created_at' 없음"
            t = real_layer.tier_of(r["created_at"])
            assert t == "hot", (
                f"실제 DB hot 쿼리에 non-hot 레코드 포함: {r['created_at']} -> tier={t}"
            )


# -- 테스트 3: tier_bounds() --------------------------------------------------

class TestTierBounds:
    """tier_bounds() -- after/before 경계 정확성."""

    def test_hot_bounds(self, layer: MemoryLayer) -> None:
        """hot -> after 있음, before 없음."""
        bounds = layer.tier_bounds("hot")
        assert bounds["after"] is not None,  "hot: after가 None이면 안 됨"
        assert bounds["before"] is None,     "hot: before가 None이어야 함"

    def test_warm_bounds(self, layer: MemoryLayer) -> None:
        """warm -> after 있음, before 없음."""
        bounds = layer.tier_bounds("warm")
        assert bounds["after"] is not None,  "warm: after가 None이면 안 됨"
        assert bounds["before"] is None,     "warm: before가 None이어야 함"

    def test_cold_bounds(self, layer: MemoryLayer) -> None:
        """cold -> after 없음, before 있음."""
        bounds = layer.tier_bounds("cold")
        assert bounds["after"] is None,      "cold: after가 None이어야 함"
        assert bounds["before"] is not None, "cold: before가 None이면 안 됨"

    def test_hot_after_is_recent(self, layer: MemoryLayer) -> None:
        """hot의 after가 now - hot_hours 근방인지 확인."""
        bounds    = layer.tier_bounds("hot")
        after_str = bounds["after"]
        assert after_str is not None
        after_dt = datetime.fromisoformat(after_str)
        if after_dt.tzinfo is None:
            after_dt = after_dt.replace(tzinfo=timezone.utc)
        now  = datetime.now(timezone.utc)
        diff = abs((now - after_dt) - timedelta(hours=layer.config.hot_hours))
        assert diff.total_seconds() < 5, (
            f"hot after 경계가 now-hot_hours에서 {diff.total_seconds():.1f}초 벗어남"
        )

    def test_invalid_tier_raises(self, layer: MemoryLayer) -> None:
        """잘못된 tier -> ValueError 발생."""
        with pytest.raises(ValueError, match="알 수 없는 tier"):
            layer.tier_bounds("unknown")  # type: ignore[arg-type]


# -- 테스트 4: summarize() ---------------------------------------------------

class TestSummarize:
    """summarize() -- hot/warm/cold 총계 정합성."""

    def test_summary_structure(self, layer: MemoryLayer) -> None:
        """반환 구조가 hot/warm/cold 세 키를 모두 가짐."""
        summary = layer.summarize()
        for tier_key in ("hot", "warm", "cold"):
            assert tier_key in summary,           f"'{tier_key}' 키 없음"
            assert "takes" in summary[tier_key],  f"'{tier_key}.takes' 없음"
            assert "pages" in summary[tier_key],  f"'{tier_key}.pages' 없음"

    def test_takes_total_matches_db(self, layer: MemoryLayer, temp_db: str) -> None:
        """hot + warm_only + cold = DB 전체 takes 수."""
        summary = layer.summarize()
        total_summary = (
            summary["hot"]["takes"]
            + summary["warm"]["takes"]
            + summary["cold"]["takes"]
        )
        conn     = sqlite3.connect(temp_db)
        total_db = conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0]
        conn.close()
        assert total_summary == total_db, (
            f"takes 합계 불일치: summary={total_summary}, DB={total_db}"
        )

    def test_pages_total_matches_db(self, layer: MemoryLayer, temp_db: str) -> None:
        """hot + warm_only + cold = DB 전체 pages 수."""
        summary = layer.summarize()
        total_summary = (
            summary["hot"]["pages"]
            + summary["warm"]["pages"]
            + summary["cold"]["pages"]
        )
        conn     = sqlite3.connect(temp_db)
        total_db = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.close()
        assert total_summary == total_db, (
            f"pages 합계 불일치: summary={total_summary}, DB={total_db}"
        )

    def test_hot_counts(self, layer: MemoryLayer) -> None:
        """hot: takes=2, pages=1 (임시 DB 기준)."""
        s = layer.summarize()
        assert s["hot"]["takes"] == 2, f"hot takes 2 기대, 실제 {s['hot']['takes']}"
        assert s["hot"]["pages"] == 1, f"hot pages 1 기대, 실제 {s['hot']['pages']}"

    def test_single_tier_summarize(self, layer: MemoryLayer) -> None:
        """tier='hot' 지정 시 hot 키만 반환."""
        s = layer.summarize(tier="hot")
        assert list(s.keys()) == ["hot"], f"hot만 기대, 실제 키: {list(s.keys())}"


# -- 테스트 5: kb_read(tier="hot") syscall 연동 ------------------------------

class TestKbReadTierRouting:
    """syscall.kb_read()에서 tier 라우팅 시간 필터 적용 확인 (완료 기준 #6)."""

    @pytest.fixture(scope="class")
    def kernel(self, temp_db: str) -> KernelAPI:
        """임시 DB KernelAPI 픽스처."""
        return KernelAPI(brain_db=temp_db)

    def test_kb_read_hot_returns_list(self, kernel: KernelAPI) -> None:
        """kb_read(tier='hot') -> list 반환 (오류 없음)."""
        results = kernel.kb_read("hot", agent="test", tier="hot", limit=5)
        assert isinstance(results, list), "kb_read 반환값이 list가 아님"

    def test_kb_read_cold_returns_list(self, kernel: KernelAPI) -> None:
        """kb_read(tier='cold') -> list 반환."""
        results = kernel.kb_read("cold", agent="test", tier="cold", limit=5)
        assert isinstance(results, list)

    def test_kb_read_auto_no_filter(self, kernel: KernelAPI) -> None:
        """kb_read(tier='auto') -> 시간 필터 없이 전체 검색."""
        results = kernel.kb_read("content", agent="test", tier="auto", limit=10)
        assert isinstance(results, list)

    def test_kb_read_explicit_after_overrides_tier(self, kernel: KernelAPI) -> None:
        """after 명시 시 tier 변환 건너뜀 -- 오류 없이 list 반환."""
        now      = datetime.now(timezone.utc)
        after_ts = (now - timedelta(hours=100)).isoformat()
        results  = kernel.kb_read(
            "content", agent="test",
            tier="hot",
            after=after_ts,
            limit=5,
        )
        assert isinstance(results, list)

    def test_real_db_kb_read_hot(self) -> None:
        """실제 brain.db kb_read(tier='hot') 시간 필터 확인 (완료 기준 #6)."""
        if not REAL_BRAIN_DB.exists():
            pytest.skip("실제 brain.db 없음 -- 건너뜀")
        kernel  = KernelAPI(brain_db=str(REAL_BRAIN_DB))
        results = kernel.kb_read("nova", agent="test", tier="hot", limit=5)
        assert isinstance(results, list), "결과가 list 아님"
        for r in results:
            assert "page_id" in r or "id" in r, f"결과 구조 이상: {r.keys()}"


# -- TierConfig 테스트 -------------------------------------------------------

class TestTierConfig:
    """TierConfig -- 설정 검증."""

    def test_default_values(self) -> None:
        """기본값: hot_hours=1.0, warm_hours=168.0."""
        cfg = TierConfig()
        assert cfg.hot_hours  == 1.0
        assert cfg.warm_hours == 168.0

    def test_custom_values(self) -> None:
        """커스텀 값 설정."""
        cfg = TierConfig(hot_hours=2.0, warm_hours=72.0)
        assert cfg.hot_hours  == 2.0
        assert cfg.warm_hours == 72.0

    def test_invalid_hot_zero_raises(self) -> None:
        """hot_hours=0 -> ValueError."""
        with pytest.raises(ValueError):
            TierConfig(hot_hours=0)

    def test_warm_le_hot_raises(self) -> None:
        """warm_hours <= hot_hours -> ValueError."""
        with pytest.raises(ValueError):
            TierConfig(hot_hours=5.0, warm_hours=3.0)

    def test_from_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """환경변수 없을 때 기본값 사용."""
        monkeypatch.delenv("NOVA_HOT_HOURS",  raising=False)
        monkeypatch.delenv("NOVA_WARM_HOURS", raising=False)
        cfg = TierConfig.from_env()
        assert cfg.hot_hours  == 1.0
        assert cfg.warm_hours == 168.0

    def test_from_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """환경변수로 오버라이드."""
        monkeypatch.setenv("NOVA_HOT_HOURS",  "2.5")
        monkeypatch.setenv("NOVA_WARM_HOURS", "336.0")
        cfg = TierConfig.from_env()
        assert cfg.hot_hours  == 2.5
        assert cfg.warm_hours == 336.0
