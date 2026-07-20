"""cron_engine.py — NOVA 시간 기반 자율 트리거

사용자 대화 없이도 NOVA가 자율적으로 지식을 확장하도록
시간 기반 harness 트리거를 제공한다.

개선 (2026-07-19):
  _last_triggered를 파일에 영속화 — watcher 재시작 후 쿨다운 유지
  takes_age 조건 완화 — 활성 takes가 있어도 일정 시간마다 health 실행
"""
from __future__ import annotations

import json
import time
import sqlite3
from pathlib import Path

# 트리거 쿨다운 (초)
_CRON_INTERVALS = {
    "health":       24 * 3600,   # 24h: health harness (건강 체크)
    "research":     48 * 3600,   # 48h: research harness (지식 확장)
    "mms_research": 72 * 3600,   # 72h: MMS 실무 데이터
}

# 영속 상태 파일 경로 (watcher 재시작 후에도 쿨다운 유지)
_STATE_PATH = Path.home() / ".nova" / "logs" / "cron_triggered.json"


def _load_state() -> dict[str, float]:
    """영속 파일에서 마지막 트리거 시각 로드."""
    try:
        return {k: float(v) for k, v in json.loads(_STATE_PATH.read_text()).items()}
    except Exception:
        return {}


def _save_state(state: dict[str, float]) -> None:
    """마지막 트리거 시각을 파일에 저장."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


# 모듈 로드 시 영속 상태 복원 + cold start 방어
_last_triggered: dict[str, float] = _load_state()
if not _STATE_PATH.exists():
    _save_state({})  # cold start 방어: 파일 선생성으로 재시작 후 즉시 발화 방지


def _get_last_takes_ts(brain_db: str) -> float:
    """brain.db에서 가장 최근 takes 생성 시간 반환 (Unix timestamp)."""
    try:
        db = sqlite3.connect(brain_db, timeout=3)
        row = db.execute(
            "SELECT created_at FROM takes ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        db.close()
        if row:
            import datetime
            dt = datetime.datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            return dt.timestamp()
    except Exception:
        pass
    return time.time()  # 오류 시 현재 시간 반환 (트리거 억제)


def cron_tick(
    brain_db: str,
    run_harness_fn,  # callable(harness_name: str) -> bool
    log_fn=print,
) -> list[str]:
    """1시간마다 호출. 조건 충족 시 harness를 자율 트리거.

    Args:
        brain_db: brain.db 경로
        run_harness_fn: harness를 실행하는 콜백 (brain_watcher 제공)
        log_fn: 로그 출력 함수

    Returns:
        트리거된 harness 이름 목록
    """
    global _last_triggered
    now = time.time()
    triggered = []

    last_takes_ts = _get_last_takes_ts(brain_db)
    takes_age_h   = (now - last_takes_ts) / 3600

    for harness_name, interval_s in _CRON_INTERVALS.items():
        last = _last_triggered.get(harness_name, 0.0)
        if (now - last) < interval_s:
            continue  # 쿨다운 미경과 (영속 상태로 재시작 후에도 보존)

        # takes_age 조건: health는 기준 완화 (12h 이상이면 실행)
        # research/mms_research는 24h+ 비활성 시에만 트리거
        min_takes_age = {
            "health":       12.0,   # health는 12h 이상 비활성 시 (원래 24h 대신)
            "research":     24.0,
            "mms_research": 24.0,
        }.get(harness_name, interval_s / 3600)

        if takes_age_h < min_takes_age:
            continue  # takes가 최근 — 트리거 불필요

        log_fn(f"[cron_engine] 시간 트리거: {harness_name} (takes {takes_age_h:.1f}h 전)")
        try:
            ok = run_harness_fn(harness_name)
            if ok:
                _last_triggered[harness_name] = now
                _save_state(_last_triggered)  # 즉시 영속화
                triggered.append(harness_name)
                log_fn(f"[cron_engine] {harness_name} 완료 → 상태 저장")
            else:
                log_fn(f"[cron_engine] {harness_name} FAIL")
        except Exception as e:
            log_fn(f"[cron_engine] {harness_name} 예외: {e}")

    return triggered
