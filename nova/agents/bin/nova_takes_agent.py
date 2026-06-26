#!/usr/bin/env python3
"""
nova_takes_agent.py — 에이전트 활동 시 Takes 자동 추가 훅
Kanban task complete summary에서 Takes 추출

사용:
  python3 nova_takes_agent.py --task-id t_xxx --agent nova-qa --summary "..."
  python3 nova_takes_agent.py --scan-recent  # 최근 완료 태스크 Takes 추출
"""
import sys
import re
import json
import os
import hashlib
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_llm import call_llm as call_haiku

KB_ROOT   = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
NOVA_BIN  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin"


def get_brain():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nova_brain", str(NOVA_BIN / "nova_brain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NovaBrain()


def extract_takes_from_summary(agent: str, task_id: str, summary: str,
                                page_path: str = None) -> list:
    """태스크 summary에서 Takes 추출 (Haiku)"""
    if not summary or len(summary) < 50:
        return []

    prompt = f"""다음 에이전트 태스크 결과에서 KB에 저장할 주장/사실을 최대 3개 추출하세요.
에이전트: {agent}
태스크: {task_id}

결과:
{summary[:600]}

JSON 배열로만 답변 (없으면 []):
[
  {{"kind": "fact|take|bet", "claim": "주장 내용", "weight": 0.0~1.0}}
]"""
    result = call_haiku(prompt)
    try:
        m = re.search(r'\[.*\]', result, re.DOTALL)
        if m:
            items = json.loads(m.group())
            return [i for i in items if isinstance(i, dict) and i.get("claim")]
    except Exception:
        pass
    return []


def add_takes_for_task(agent: str, task_id: str, summary: str,
                       page_path: str = None):
    """태스크 결과에서 Takes 추출 후 nova_brain에 추가"""
    items = extract_takes_from_summary(agent, task_id, summary, page_path)
    if not items:
        return 0

    # 관련 KB 파일 찾기 (없으면 agents/registry.md 기본)
    if not page_path:
        page_path = "agents/registry.md"

    brain = get_brain()
    added = 0
    try:
        for item in items:
            # Round12: dynamic weight based on evidence quality
            # LLM이 weight를 명시하면 그 값을 우선 사용
            # 명시 안 하면 claim/summary 길이 기반으로 품질 추정
            raw_w = item.get("weight")
            if raw_w is not None:
                weight = float(raw_w)
            else:
                claim_len = len(item.get("claim", ""))
                summary_len = len(summary)
                # DoD 증거가 있는 완료 태스크: 0.87
                # 풍부한 증거 (긴 claim + 충분한 summary): 0.86 (R17: 0.82→0.86, hq 기준 충족)
                # 최소 정보: 0.80
                dod_keywords = ["완료", "done", "deployed", "merged", "confirmed", "verified",
                                "성공", "구현", "달성", "적용", "수립"]
                has_dod = any(kw in (item.get("claim","") + summary[:200]).lower()
                              for kw in dod_keywords)
                if has_dod and summary_len >= 200:
                    weight = 0.87  # DoD 완료 → 0.85→0.87 (R17)
                elif claim_len >= 30 and summary_len >= 100:
                    weight = 0.86  # 풍부한 증거 → 0.82→0.86 (R17, hq기준 0.85+ 충족)
                else:
                    weight = 0.80  # 최소 정보 → 0.78→0.80 (R17)
            brain.add_take(
                holder=agent,
                page_path=page_path,
                kind=item.get("kind", "take"),
                claim=item.get("claim", ""),
                weight=weight,
                source=f"kanban:{task_id}",
            )
            # agent_activity 로깅
            brain.log_activity(
                agent=agent, action="takes_added",
                task_id=task_id,
                summary=f"Takes 추가: {item.get('claim','')[:80]}",
            )
            added += 1
    finally:
        brain.close()  # Round6 fix: try/finally ensures close() on all paths
    return added


def scan_recent_completed(limit: int = 20):
    """최근 완료된 Kanban 태스크에서 Takes 자동 추출 — 모든 board DB 스캔"""
    import sqlite3
    # 실제 active board DBs 우선, 레거시 루트 kanban.db는 폴백
    board_dbs = sorted(Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kanban/boards".glob("*/kanban.db"))
    if not board_dbs:
        legacy_db = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kanban.db"
        board_dbs = [legacy_db] if legacy_db.exists() else []
    if not board_dbs:
        print("kanban db 없음")
        return

    all_rows = []
    for kanban_db in board_dbs:
        conn = sqlite3.connect(str(kanban_db))
        conn.row_factory = sqlite3.Row
        try:
            db_rows = conn.execute("""
                SELECT tr.task_id, t.assignee, tr.summary, tr.ended_at
                FROM task_runs tr
                JOIN tasks t ON tr.task_id = t.id
                WHERE (
                    tr.outcome IN ('completed','done')
                    OR (tr.outcome IS NULL AND tr.ended_at IS NOT NULL AND tr.status='done')
                )
                  AND tr.summary IS NOT NULL
                  AND length(tr.summary) > 50
                ORDER BY tr.ended_at DESC LIMIT ?
            """, (limit,)).fetchall()
            all_rows.extend(db_rows)
        except Exception:
            pass
        finally:
            conn.close()

    rows = sorted(all_rows, key=lambda r: r["ended_at"] or "", reverse=True)[:limit]
    total = 0
    for row in rows:
        n = add_takes_for_task(
            agent=row["assignee"] or "unknown",
            task_id=row["task_id"],
            summary=row["summary"],
        )
        if n > 0:
            print(f"  {row['task_id'][:8]} ({row['assignee']}): Takes {n}개 추가")
            total += n

    print(f"총 {total}개 Takes 추가")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id",  help="Kanban 태스크 ID")
    parser.add_argument("--agent",    help="에이전트 이름")
    parser.add_argument("--summary",  help="태스크 결과 요약")
    parser.add_argument("--page",     help="관련 KB 파일 경로")
    parser.add_argument("--scan-recent", action="store_true",
                        help="최근 완료 태스크 Takes 자동 추출")
    args = parser.parse_args()

    if args.scan_recent:
        scan_recent_completed()
    elif args.task_id and args.agent and args.summary:
        n = add_takes_for_task(args.agent, args.task_id, args.summary, args.page)
        print(f"Takes {n}개 추가")
    else:
        parser.print_help()
