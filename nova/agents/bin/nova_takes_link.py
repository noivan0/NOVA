#!/usr/bin/env python3
"""
nova_takes_link.py — orphan takes → pages 자동연결 엔진
=======================================================
트리거: brain_watcher → orphan_pages >= 3 시 fix_orphan 대신/추가로 실행
         또는 수동 실행: python3 nova_takes_link.py

문제:
  takes.page_id = NULL인 orphan takes가 누적됨 (hermes insight/pattern 등)
  brain_watcher는 pages.agent=NULL인 orphan pages만 감시하고
  takes orphan(page_id=NULL)은 감시/수정하지 않음
  → score_coverage 낮음: 연결된 takes가 없는 페이지가 coverage에 불리

해결:
  1. page_chunks 내용을 기반으로 BM25/키워드 매칭
  2. 한국어/영어 모두 지원 (brain_watcher fix_orphan이 한국어에 불리했음)
  3. 임계값 기반 연결 (0.2 이상)
  4. 연결 후 brain_health 갱신 트리거 (nudge)
"""

from __future__ import annotations
import os
import re
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME    = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
BRAIN_DB     = NOVA_HOME / "brain.db"
LOG_FILE     = NOVA_HOME / "logs" / "takes_link.log"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[takes-link] [{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _tokenize(text: str) -> list[str]:
    """한국어 + 영어 토큰화. 2자 이상만."""
    # 영어: 소문자 단어
    eng = re.findall(r"[a-z]{2,}", text.lower())
    # 한국어: 2자 이상 한글 문자열
    kor = re.findall(r"[가-힣]{2,}", text)
    # 알파뉴메릭 식별자 (snake_case, camelCase 등)
    ids = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text)
    return eng + kor + ids


def _bm25_score(query_tokens: list[str], doc_tokens: list[str],
                k1: float = 1.5, b: float = 0.75,
                avg_dl: float = 100.0) -> float:
    """BM25 스코어링 (한국어 지원)."""
    if not query_tokens or not doc_tokens:
        return 0.0
    dl = len(doc_tokens)
    doc_counter = Counter(doc_tokens)
    score = 0.0
    for qt in set(query_tokens):
        tf = doc_counter.get(qt, 0)
        if tf == 0:
            continue
        idf = 1.0  # 단순화 (전체 corpus IDF는 생략)
        tf_norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg_dl))
        score += idf * tf_norm
    return score


def link_orphan_takes(db: sqlite3.Connection, threshold: float = 0.2,
                      max_per_run: int = 50) -> int:
    """orphan takes를 page_chunks와 매칭하여 연결. 연결 수 반환."""
    # orphan takes 로드
    orphans = db.execute(
        "SELECT id, claim, kind, holder FROM takes WHERE page_id IS NULL LIMIT ?",
        (max_per_run * 2,)
    ).fetchall()

    if not orphans:
        return 0

    # page_chunks 로드 (content + page_id)
    chunks = db.execute(
        "SELECT page_id, content FROM page_chunks WHERE content IS NOT NULL"
    ).fetchall()

    if not chunks:
        return 0

    # 청크별 토큰 사전 계산
    chunk_tokens = [(pid, _tokenize(content or "")) for pid, content in chunks]
    avg_dl = sum(len(toks) for _, toks in chunk_tokens) / max(len(chunk_tokens), 1)

    linked = 0
    for take_id, claim, kind, holder in orphans:
        query_toks = _tokenize(claim or "")
        if len(query_toks) < 2:
            continue

        # 베스트 매칭 페이지 찾기
        best_pid = None
        best_score = 0.0
        for page_id, doc_toks in chunk_tokens:
            score = _bm25_score(query_toks, doc_toks, avg_dl=avg_dl)
            if score > best_score:
                best_score = score
                best_pid = page_id

        if best_score >= threshold and best_pid:
            db.execute(
                "UPDATE takes SET page_id=?, source=COALESCE(source,'takes_link') WHERE id=?",
                # BUG-SOURCE-NULL-4 (2026-07-31): orphan 연결 시 source 기록
                (best_pid, take_id)
            )
            linked += 1
            if linked % 10 == 0:
                db.commit()

    db.commit()
    return linked


def nudge_brain_watcher(db: sqlite3.Connection) -> None:
    """brain_watcher에 WAL 이벤트 전송 → _react() 트리거."""
    try:
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO hermes_events "
            "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "TAKES_LINKED", "info", "orphan takes 연결 완료",
             "takes_link engine", "nova-takes-link", 0, now)
        )
        db.execute("DELETE FROM hermes_events WHERE event_type='TAKES_LINKED'")
        db.commit()
    except Exception as e:
        _log(f"WARN: nudge 실패: {e}")


def main() -> None:
    _log("=== takes_link 시작 ===")

    if not BRAIN_DB.exists():
        _log(f"SKIP: {BRAIN_DB} 없음")
        return

    db = sqlite3.connect(str(BRAIN_DB), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")

    try:
        before = db.execute("SELECT COUNT(*) FROM takes WHERE page_id IS NULL").fetchone()[0]
        _log(f"orphan takes: {before}개")

        linked = link_orphan_takes(db)

        after = db.execute("SELECT COUNT(*) FROM takes WHERE page_id IS NULL").fetchone()[0]
        _log(f"연결 완료: {linked}개 연결 ({before} → {after} orphan 잔존)")

        if linked > 0:
            nudge_brain_watcher(db)
            _log("brain_watcher nudge 완료 → coverage 재계산 예정")
    finally:
        db.close()

    _log("=== takes_link 완료 ===")


if __name__ == "__main__":
    main()
