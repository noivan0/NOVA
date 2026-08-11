#!/usr/bin/env python3
"""
nova_fix_orphan.py — orphan pages agent 자동 배정 엔진
========================================================
BUG-CRITICAL-1: 기존 fix_orphan.py = nova_brain.py 복사본 (기능 없음)
→ 독립 스크립트로 교체: wiki/memories pages에 agent 자동 배정

트리거: brain_watcher → orphan_pages >= 3 시 30초 쿨다운으로 실행

동작:
1. pages WHERE agent IS NULL AND page_type='general' 쿼리
2. path prefix 기반 agent 자동 배정:
   - wiki/  → agent='wiki'
   - memories/ → agent='memory'  
   - kb/ → agent='knowledge'
   - workspace/ → agent='harness'
3. UPDATE pages SET agent=? WHERE path=?  ← id=NULL 대응 (path 기반)
4. brain.db nudge (WAL write → inotify CLOSE_WRITE)
"""

from __future__ import annotations
import os
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
BRAIN_DB    = NOVA_HOME / "brain.db"
LOG_FILE    = NOVA_HOME / "logs" / "fix_orphan.log"

# path prefix → agent 매핑
AGENT_BY_PREFIX = [
    ("nova_workspace/",   "harness"),   # CRITICAL-3 FIX: 6175개 최대 orphan prefix
    ("wiki/",             "wiki"),
    ("memories/",         "memory"),
    ("workspace/",        "harness"),
    ("kb/audit",          "audit"),
    ("kb/config",         "config"),
    ("kb/agents/",        None),        # None = 경로에서 에이전트명 추출
    ("kb/lessons/",       "knowledge"),
    ("kb/memory_archive/","knowledge"),
    ("kb/nova/",          "knowledge"),
    ("kb/projects/",      "knowledge"),
    ("kb/",               "knowledge"),
    # 추가: orphan 잔존 경로 매핑 (2026-07-10)
    ("projects/",         "knowledge"),
    ("config/",           "knowledge"),
    ("fixes/",            "knowledge"),
    ("user/",             "knowledge"),
    ("weekly/",           "knowledge"),
    ("nova_synthesis/",   "knowledge"),
    ("nova_wiki",         "wiki"),
    ("nova_agent",        "knowledge"),
    ("nova_brain",        "knowledge"),
    ("nova_",             "knowledge"),  # 나머지 nova_ prefix 폴백
    # KB 하위 디렉토리 경로 (kb/ prefix 없이 저장된 경우)
    ("agents/",           None),        # agents/<agent-name>/ → agent명 추출
    ("lessons/",          "knowledge"),
    ("memory_archive/",   "knowledge"),
    ("audit_loop/",       "audit"),
]


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[fix-orphan] [{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get_agent(path_str: str) -> str | None:
    """path prefix 기반 agent 자동 배정."""
    for prefix, agent in AGENT_BY_PREFIX:
        if path_str.startswith(prefix):
            if agent is None:
                # kb/agents/<agent-name>/ 또는 agents/<agent-name>/ 패턴 추출
                parts = path_str.split("/")
                if len(parts) >= 2 and parts[-2 if len(parts) == 2 else 1].startswith("nova-"):
                    # agents/nova-xxx/ 또는 kb/agents/nova-xxx/
                    agent_idx = 2 if parts[0] == "kb" else 1
                    if len(parts) > agent_idx:
                        return parts[agent_idx]
                if len(parts) >= 3 and parts[0] == "kb" and parts[1] == "agents":
                    return parts[2]
                if len(parts) >= 2 and parts[0] == "agents":
                    return parts[1]
            return agent
    return "general"  # 기본값


def main() -> None:
    _log("=== fix_orphan 시작 ===")

    if not BRAIN_DB.exists():
        _log(f"SKIP: {BRAIN_DB} 없음")
        return

    remaining = -1  # MINOR-1 FIX: DB 오류 시 NameError 방지
    db = sqlite3.connect(str(BRAIN_DB), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")

    try:
        # orphan pages 조회 — page_type 무관하게 agent IS NULL 전체 처리
        # BUG-FIX: page_type='general'만 처리했던 것을 kb/workspace/synthesis 타입까지 확장
        rows = db.execute(
            "SELECT id, path FROM pages WHERE agent IS NULL"
        ).fetchall()

        _log(f"orphan pages: {len(rows)}개")

        updated = 0
        for page_id, path_str in rows:
            agent = _get_agent(str(path_str))
            if agent:
                # CRITICAL-2 FIX: id=NULL 행은 WHERE id=? 가 NULL=NULL=FALSE로 0 rows
                # → path 기반 UPDATE + rowcount 확인으로 허위 성공 보고 차단
                cursor = db.execute(
                    "UPDATE pages SET agent=? WHERE path=?",
                    (agent, path_str)
                )
                if cursor.rowcount > 0:
                    updated += 1

        if updated > 0:
            db.commit()
            _log(f"agent 배정 완료: {updated}개")

            # brain.db nudge → brain_watcher inotify 재트리거
            eid = uuid.uuid4().hex[:16]
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT OR IGNORE INTO hermes_events "
                "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eid, "FIX_ORPHAN", "info", f"orphan {updated}개 agent 배정 완료",
                 str([str(r[1]) for r in rows[:5]]), "nova-fix-orphan", 0, now)
            )
            db.execute("DELETE FROM hermes_events WHERE event_type='FIX_ORPHAN'")
            db.commit()
        else:
            _log("배정할 orphan 없음")

        # 최종 agent_activities 출력 (기존 stats 호환)
        acts = db.execute("SELECT COUNT(*) FROM agent_activity").fetchone()[0]
        remaining = db.execute("SELECT COUNT(*) FROM pages WHERE agent IS NULL").fetchone()[0]
        _log(f"  agent_activities: {acts}")
        print(f"  agent_activities: {acts}")  # brain_watcher 로그 파싱용

    finally:
        db.close()

    _log(f"=== fix_orphan 완료 (orphan 잔존: {remaining}) ===")


if __name__ == "__main__":
    main()
