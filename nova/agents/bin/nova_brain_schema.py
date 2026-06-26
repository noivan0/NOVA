import os
#!/usr/bin/env python3
"""
nova_brain.db — NOVA 통합 지식 브레인 DB
sqlite-vec 기반 벡터 검색 + 컴파일드 트루스 + Takes/Bet + 모순 감지

구조:
  pages            — KB 페이지 (컴파일드 트루스 + 타임라인 분리)
  page_chunks      — 청크별 임베딩 (sqlite-vec)
  takes            — 신념/주장 추적 (fact/take/bet/hunch)
  contradictions   — 모순 감지 캐시
  brain_health     — 헬스 점수 이력
  agent_activity   — 에이전트 활동 로그
"""
import sqlite3
import sqlite_vec
import struct
import json
import hashlib
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

NOVA_BRAIN_PATH = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "nova_brain.db"
EMBED_DIMS = 3072  # text-embedding-3-large


def get_conn(path: Path = NOVA_BRAIN_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB
    return conn


def init_schema(conn: sqlite3.Connection):
    conn.executescript(f"""
    -- ============================================================
    -- 1. PAGES — 컴파일드 트루스 + 타임라인 분리
    -- ============================================================
    CREATE TABLE IF NOT EXISTS pages (
        id              TEXT PRIMARY KEY,          -- SHA256(path)[:16]
        path            TEXT NOT NULL UNIQUE,       -- kb/ 상대 경로
        title           TEXT,
        agent           TEXT,                       -- 작성 에이전트
        page_type       TEXT DEFAULT 'general',     -- project/concept/entity/agent/general
        -- GBrain 핵심 패턴: 두 구역 분리
        compiled_truth  TEXT,                       -- 현재 최선의 이해 (덮어쓰기)
        timeline        TEXT,                       -- 추가전용 증거 흔적 (절대 편집 안 함)
        -- 메타
        content_hash    TEXT,
        char_count      INTEGER,
        indexed_at      TEXT,
        updated_at      TEXT,
        -- 헬스
        health_score    REAL DEFAULT 1.0,           -- 0~1, 낮을수록 개선 필요
        has_contradictions INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pages_agent ON pages(agent);
    CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type);
    CREATE INDEX IF NOT EXISTS idx_pages_health ON pages(health_score);

    -- ============================================================
    -- 2. PAGE_CHUNKS — 청크별 벡터 (sqlite-vec)
    -- ============================================================
    CREATE TABLE IF NOT EXISTS page_chunks (
        id          TEXT PRIMARY KEY,
        page_id     TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
        chunk_idx   INTEGER NOT NULL,
        section     TEXT,                           -- compiled_truth / timeline / other
        content     TEXT NOT NULL,
        char_count  INTEGER,
        indexed_at  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_page ON page_chunks(page_id);

    -- sqlite-vec 가상 테이블 (벡터 검색)
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
        chunk_id TEXT PRIMARY KEY,
        embedding float[{EMBED_DIMS}]
    );

    -- ============================================================
    -- 3. TAKES — 신념/주장 추적 (GBrain Takes 시스템)
    -- ============================================================
    CREATE TABLE IF NOT EXISTS takes (
        id          TEXT PRIMARY KEY,
        page_id     TEXT REFERENCES pages(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK(kind IN ('fact','take','bet','hunch')),
        holder      TEXT NOT NULL,                  -- 주장한 에이전트
        claim       TEXT NOT NULL,                  -- 주장 내용
        weight      REAL DEFAULT 0.5,               -- 확신도 0~1
        source      TEXT,                           -- 근거 KB 경로 또는 URL
        evidence    TEXT,                           -- 구체적 증거
        valid_from  TEXT,                           -- 유효 시작일
        valid_until TEXT,                           -- 유효 종료일
        superseded_by TEXT,                         -- 취소선: 대체된 take id
        outcome     TEXT,                           -- 실제 결과 (bet 판정 후)
        brier_score REAL,                           -- 예측 정확도 (bet 전용)
        created_at  TEXT NOT NULL,
        updated_at  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_takes_page ON takes(page_id);
    CREATE INDEX IF NOT EXISTS idx_takes_holder ON takes(holder);
    CREATE INDEX IF NOT EXISTS idx_takes_kind ON takes(kind);
    CREATE INDEX IF NOT EXISTS idx_takes_active ON takes(superseded_by)
        WHERE superseded_by IS NULL;

    -- ============================================================
    -- 4. CONTRADICTIONS — 모순 감지 캐시
    -- ============================================================
    CREATE TABLE IF NOT EXISTS contradictions (
        id              TEXT PRIMARY KEY,
        page_id_a       TEXT NOT NULL,
        page_id_b       TEXT NOT NULL,
        claim_a         TEXT NOT NULL,
        claim_b         TEXT NOT NULL,
        severity        TEXT DEFAULT 'medium'       -- critical/high/medium/low
                        CHECK(severity IN ('critical','high','medium','low')),
        status          TEXT DEFAULT 'open'         -- open/resolved/dismissed
                        CHECK(status IN ('open','resolved','dismissed')),
        resolution      TEXT,
        detected_at     TEXT NOT NULL,
        resolved_at     TEXT,
        detected_by     TEXT DEFAULT 'nova-qa'
    );
    CREATE INDEX IF NOT EXISTS idx_contradictions_status ON contradictions(status);
    CREATE INDEX IF NOT EXISTS idx_contradictions_pages
        ON contradictions(page_id_a, page_id_b);

    -- ============================================================
    -- 5. BRAIN_HEALTH — 헬스 점수 이력
    -- ============================================================
    CREATE TABLE IF NOT EXISTS brain_health (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        measured_at     TEXT NOT NULL,
        measured_by     TEXT DEFAULT 'nova-evaluator',
        -- 도메인별 점수
        score_overall   REAL,                       -- 0~100
        score_coverage  REAL,                       -- KB 커버리지
        score_freshness REAL,                       -- 최신성
        score_consistency REAL,                     -- 모순 없음
        score_depth     REAL,                       -- 청크당 평균 깊이
        -- 상세
        total_pages     INTEGER,
        pages_with_takes INTEGER,
        open_contradictions INTEGER,
        orphan_pages    INTEGER,
        stale_pages     INTEGER,
        -- 메타
        notes           TEXT,
        thresholds_crossed TEXT                     -- JSON 배열
    );

    -- ============================================================
    -- 6. AGENT_ACTIVITY — 에이전트 활동 로그
    -- ============================================================
    CREATE TABLE IF NOT EXISTS agent_activity (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agent       TEXT NOT NULL,
        task_id     TEXT,                           -- Kanban task id
        action      TEXT NOT NULL,                  -- read/write/search/complete
        target_path TEXT,
        summary     TEXT,
        tokens_used INTEGER DEFAULT 0,
        duration_s  REAL,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_activity_agent ON agent_activity(agent);
    CREATE INDEX IF NOT EXISTS idx_activity_task ON agent_activity(task_id);

    -- ============================================================
    -- 7. TRAJECTORIES — 엔티티 시계열 메트릭 (GBrain 궤적 추적)
    -- ============================================================
    CREATE TABLE IF NOT EXISTS trajectories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id     TEXT REFERENCES pages(id),
        metric      TEXT NOT NULL,                  -- "krayt_run_methods", "pnl_30d" 등
        value       REAL,
        unit        TEXT,
        period      TEXT,                           -- "2026-05-21"
        source      TEXT,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_traj_page_metric ON trajectories(page_id, metric);
    CREATE INDEX IF NOT EXISTS idx_traj_period ON trajectories(period);
    """)
    conn.commit()


if __name__ == "__main__":
    conn = get_conn()
    init_schema(conn)
    # 검증
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print("nova_brain.db 초기화 완료")
    print("테이블:", tables)
    # vec0 테이블 벡터 검색 테스트
    cur.execute("SELECT vec_version()")
    print("sqlite-vec:", cur.fetchone()[0])
    conn.close()
