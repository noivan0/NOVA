#!/usr/bin/env python3
"""
NOVA 자율 감사 루프 오케스트레이터
- 3개 앱 kanban 보드에서 ready 태스크를 꺼내서 해당 NOVA 에이전트 프로필로 실행
- 매 2시간마다 자동 실행
- 결과는 KB에 저장 + 이슈 발견 시 헤르 운영 토픽(thread_id=9)으로 알림
"""
import subprocess, os, json, re, shlex
from datetime import datetime
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
# BOARDS: nova_boards.json에서 동적 로드
import json as _json, pathlib as _pl
_boards_file = _pl.Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kanban/nova_boards.json"
BOARDS = _json.load(open(_boards_file))["boards"] if _boards_file.exists() else []
MAX_TASKS_PER_RUN = 3  # 한 번에 최대 3개 태스크 처리

env = os.environ.copy()
env["PATH"] = f"{HERMES_HOME}/hermes-agent/venv/bin:/root/.local/bin:" + env.get("PATH", "")
env["HERMES_HOME"] = HERMES_HOME

def run(cmd, **kwargs):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, **kwargs)
    return r.stdout + r.stderr

def get_ready_tasks(board):
    """보드에서 ready 상태 태스크 목록 가져오기"""
    # SECURITY (2026-08-28): board 값은 nova_boards.json(로컬 설정파일)에서
    # 오지만, 셸 명령 문자열에 f-string으로 직접 삽입하는 패턴 자체가
    # Black Hat 2026 Check Point 리서치(prompt-controlled/untrusted content
    # crossing into trusted framework logic)와 같은 클래스 위험을 남긴다.
    # shlex.quote()로 감싸 설정파일이 오염되거나 손상되어도 셸 명령 구조가
    # 깨지지 않도록 방어적으로 강화한다.
    out = run(f"hermes kanban boards switch {shlex.quote(board)} 2>/dev/null && hermes kanban list --status ready 2>&1")
    tasks = []
    for line in out.split("\n"):
        m = re.search(r"(t_[a-f0-9]+)\s+ready\s+\(([^)]+)\)\s+(.+)", line)
        if m:
            task_id, assignee, title = m.group(1), m.group(2), m.group(3).strip()
            tasks.append({"id": task_id, "assignee": assignee, "title": title, "board": board})
    return tasks

def dispatch_task(board, task):
    """태스크를 해당 NOVA 에이전트 프로필로 디스패치"""
    assignee = task["assignee"].strip().replace("(unassigned)", "").strip()
    if not assignee or assignee == "unassigned":
        return False, "미배정 태스크 스킵"
    
    # 프로필 존재 여부 확인
    profile_path = f"{HERMES_HOME}/profiles/{assignee}"
    if not os.path.exists(profile_path):
        return False, f"프로필 없음: {assignee}"
    
    # kanban dispatch로 에이전트 실행
    # task['id']는 이미 정규식 `t_[a-f0-9]+`로만 매치되어 셸 메타문자를
    # 포함할 수 없지만, board는 위와 동일한 이유로 shlex.quote() 유지.
    out = run(f"hermes kanban boards switch {shlex.quote(board)} 2>/dev/null && hermes kanban dispatch {shlex.quote(task['id'])} 2>&1")
    success = "dispatched" in out.lower() or "claiming" in out.lower() or "running" in out.lower()
    return success, out.strip()[:100]

def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{now}] NOVA 자율 감사 루프 시작")
    
    dispatched = 0
    skipped = 0
    
    for board in BOARDS:
        tasks = get_ready_tasks(board)
        nova_tasks = [t for t in tasks if t["assignee"] not in ("unassigned", "(unassigned)")]
        
        print(f"  [{board}] ready={len(tasks)}, nova배정={len(nova_tasks)}")
        
        for task in nova_tasks[:MAX_TASKS_PER_RUN]:
            if dispatched >= MAX_TASKS_PER_RUN:
                break
            ok, msg = dispatch_task(board, task)
            if ok:
                print(f"  ✅ [{task['assignee']}] {task['title'][:50]}")
                dispatched += 1
            else:
                print(f"  ⏭ 스킵: {msg}")
                skipped += 1
    
    print(f"\n완료: dispatched={dispatched}, skipped={skipped}")
    
    # 이슈 발견 태스크가 있으면 stdout 출력 (no_agent 크론 알림용)
    if dispatched > 0:
        print(f"🤖 NOVA 자율 루프: {dispatched}개 태스크 에이전트 배정 완료 ({now})")

if __name__ == "__main__":
    main()
