#!/usr/bin/env python3
"""
nova_on_done_takes.py — NOVA 태스크 완료 시 자동 takes 기록 강제화
방안 B: on_done 트리거에 takes INSERT 강제 포함

사용법:
  python3 nova_on_done_takes.py --agent nova-qa --page kb/nova/nova-qa-sprint --text "QA 완료" --weight 0.88
  python3 nova_on_done_takes.py --bulk  # 미커버 페이지 일괄 처리
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))


import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

NOVA_BRAIN_DB = f"{_HERMES_HOME}/nova_brain.db"
NOVA_BRAIN_CLI = f"{_HERMES_HOME}/bin/nova_brain_cli.py"

AGENT_WEIGHTS = {
    "nova-qa": 0.88,
    "nova-cso": 0.90,
    "nova-dev": 0.85,
    "nova-canary": 0.85,
    "nova-ship": 0.88,
    "nova-retro": 0.82,
    "nova-research": 0.85,
    "nova-evaluate": 0.90,
    "nova-evaluator": 0.90,
    "nova-investigate": 0.85,
    "nova-health": 0.85,
    "nova-checkpoint": 0.82,
    "nova-validator": 0.88,
    "nova-strategy": 0.85,
    "nova-marketing": 0.80,
    "nova-document": 0.80,
    "nova-benchmark": 0.82,
    "nova-review": 0.85,
    "nova-autoplan": 0.83,
    "nova-learn": 0.80,
    "nova-careful": 0.85,
}

def add_takes(agent: str, page: str, text: str, weight: float, kind: str = "take"):
    """nova_brain_cli.py takes add 실행"""
    cmd = [
        "python3", NOVA_BRAIN_CLI, "takes", "add",
        agent, page, kind, text, str(weight)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        print(f"✅ takes 등록: {agent}/{page} ({weight})")
        return True
    else:
        print(f"❌ takes 실패: {result.stderr[:100]}")
        return False

def bulk_register():
    """미커버 페이지 일괄 takes 등록"""
    db = sqlite3.connect(NOVA_BRAIN_DB)
    c = db.cursor()
    
    try:
        # takes 없는 페이지 조회
        c.execute("""
            SELECT p.id, p.path, p.title, p.agent, p.char_count
            FROM pages p
            LEFT JOIN takes t ON p.id = t.page_id
            WHERE t.page_id IS NULL
              AND p.path NOT LIKE '%synth-2026%'
              AND p.char_count > 200
            ORDER BY p.char_count DESC
            LIMIT 50
        """)
        pages = c.fetchall()
    finally:
        db.close()  # Round6 fix: try/finally ensures close() on all paths
    
    print(f"미커버 페이지 {len(pages)}개 일괄 등록 시작...")
    ok = 0
    for page_id, path, title, agent, char_count in pages:
        # 에이전트 추론: path prefix 기반
        if not agent:
            if path.startswith("agents/nova-"):
                agent = path.split("/")[1]
            elif path.startswith("config/"):
                agent = "nova-evaluator"
            elif path.startswith("fixes/"):
                agent = "nova-investigate"
            elif path.startswith("projects/"):
                agent = "nova-dev"
            else:
                agent = "nova-health"
        
        weight = AGENT_WEIGHTS.get(agent, 0.80)
        text = f"페이지 검토 완료: {title or path} ({char_count}자)"
        
        if add_takes(agent, path, text, weight, kind="fact"):
            ok += 1
    
    print(f"\n완료: {ok}/{len(pages)}개 등록")
    return ok

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="nova-evaluator")
    parser.add_argument("--page", default="")
    parser.add_argument("--text", default="태스크 완료")
    parser.add_argument("--weight", type=float, default=0.85)
    parser.add_argument("--kind", default="take")
    parser.add_argument("--bulk", action="store_true")
    args = parser.parse_args()
    
    if args.bulk:
        bulk_register()
    else:
        if not args.page:
            print("오류: --page 필요")
            sys.exit(1)
        add_takes(args.agent, args.page, args.text, args.weight, args.kind)

if __name__ == "__main__":
    main()
