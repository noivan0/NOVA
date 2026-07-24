#!/usr/bin/env python3
"""
roop_runner.py — /roop 명령 실행기
사용법: python3 roop_runner.py "목표 텍스트" [--sprints N]
"""
import sys, os, uuid, json, subprocess, argparse
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home()/".nova")))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes")))
BRAIN_DB    = NOVA_HOME / "brain.db"

def classify_goal(goal: str) -> dict:
    """목표 텍스트에서 DoD 자동 설계"""
    kw = goal.lower()
    if any(w in kw for w in ["완성","구현","개발","만들","build","create","implement"]):
        criteria = [
            "핵심 기능 구현 완료 (코드/파일 존재 확인)",
            "자동화된 검증 통과 (오류 없음, pytest/lint)",
            "KB 문서화 완료 (report.md 생성 및 brain.db 인덱싱)"
        ]
        pass_kw = "IMPL_PASS"
    elif any(w in kw for w in ["분석","연구","조사","리서치","research","study"]):
        criteria = [
            "KB report.md 생성 (500자 이상)",
            "핵심 발견 3개 이상 brain.db pages에 인덱싱",
            "research harness synthesis 완료"
        ]
        pass_kw = "RESEARCH_PASS"
    elif any(w in kw for w in ["수정","버그","픽스","fix","patch","debug"]):
        criteria = [
            "버그 재현 스크립트 및 수정 확인",
            "수정 전후 테스트 비교",
            "변경사항 문서화"
        ]
        pass_kw = "FIX_PASS"
    elif any(w in kw for w in ["감사","검증","audit","verify","review"]):
        criteria = [
            "감사 항목 80% 이상 PASS",
            "발견 이슈 목록 문서화",
            "수정 권고사항 brain.db 기록"
        ]
        pass_kw = "AUDIT_PASS"
    else:
        criteria = [
            "목표 산출물 존재 확인",
            "품질 기준 통과",
            "결과 문서화 완료"
        ]
        pass_kw = "GOAL_PASS"
    return {"criteria": criteria, "pass_keyword": pass_kw}

def write_kpi_prompt(goal: str, dod: dict) -> None:
    """kpi_evaluate harness evaluate.txt 동적 생성"""
    prompt_path = NOVA_HOME/"harnesses"/"kpi_evaluate"/"prompts"/"evaluate.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    criteria_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(dod["criteria"]))
    content = f"""You are nova-evaluator performing KPI evaluation for a ROOP goal.

ROOP Goal:
{goal}

DoD Criteria (all must be met for KPI_PASS):
{criteria_text}

Review the collected metrics (brain.db health, workspace artifacts, test results).

Output format:
## KPI Evaluation

### Goal Achievement
[Describe what was accomplished]

### Criteria Check
[For each criterion: PASS/FAIL + evidence]

### Final Verdict
[Summary of achievement]

KPI_PASS: [All criteria met — output this exact token if PASS]
KPI_FAIL: [Criteria not met — output this exact token if FAIL, list missing items]

IMPORTANT: Output exactly one of KPI_PASS or KPI_FAIL as the final line.
"""
    prompt_path.write_text(content, encoding="utf-8")
    print(f"  kpi_evaluate 프롬프트 업데이트: {prompt_path}")

def archive_stale_ready_tasks() -> int:
    """새 /roop 시작 전 이전 세션 잔여 ready 태스크를 archive.
    잔여 ready 태스크가 있으면 chain_engine이 의도치 않은 에이전트를 실행할 수 있음.
    """
    r = subprocess.run(
        ["hermes", "kanban", "--board", "nova-loop", "list", "--json"],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    try:
        import json as _json
        tasks = _json.loads(r.stdout)
    except Exception:
        return 0
    archived = 0
    for t in tasks:
        if t.get("status") in ("ready", "todo", "blocked"):
            tid = t.get("id", "")
            if not tid:
                continue
            ra = subprocess.run(
                ["hermes", "kanban", "--board", "nova-loop", "archive", tid],
                capture_output=True, text=True
            )
            if ra.returncode == 0:
                archived += 1
    return archived


def register_kanban_task(goal: str, dod: dict) -> str:
    """kanban nova-loop에 nova-autoplan 태스크 등록"""
    title = f"[ROOP] {goal[:60]}"
    body_lines = [f"ROOP 목표: {goal}", "", "DoD (완료 기준):"]
    for c in dod["criteria"]:
        body_lines.append(f"- {c}")
    body_lines += ["", f"KPI 통과 키워드: {dod['pass_keyword']}"]
    body = "\n".join(body_lines)
    
    r = subprocess.run(
        ["hermes", "kanban", "--board", "nova-loop", "create",
         title,
         "--body", body, "--assignee", "nova-autoplan", "--priority", "1"],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        # task id 추출
        for line in r.stdout.split("\n"):
            if "t_" in line:
                for tok in line.split():
                    if tok.startswith("t_"):
                        return tok
    return ""

def nudge_brain_watcher(goal: str) -> None:
    """brain_watcher에 ROOP_START 이벤트 → chain_engine 즉시 트리거.
    설계: INSERT(commit) → sleep → DELETE(commit) 순서로 brain_watcher 폴링 창 확보.
    원자적 INSERT+DELETE로는 brain_watcher가 이벤트를 볼 수 없음(race condition).
    """
    try:
        import sqlite3, time
        db = sqlite3.connect(str(BRAIN_DB), timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO hermes_events "
            "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "ROOP_START", "info", f"[ROOP] {goal[:60]}", goal, "hermes-roop", 0, now)
        )
        db.commit()  # ① INSERT 먼저 commit → brain_watcher 폴링 창 확보
        time.sleep(2)  # brain_watcher inotify 주기 대기
        db.execute("DELETE FROM hermes_events WHERE id=?", (eid,))  # 특정 id만 삭제
        db.commit()  # ② 별도 commit으로 cleanup
        db.close()
        print("  brain.db nudge 완료 (chain_engine 트리거)")
    except Exception as e:
        print(f"  WARN: nudge 실패: {e}")

def save_roop_state(goal: str, dod: dict, task_id: str, max_sprints: int) -> None:
    """ROOP 상태 파일 저장 (모니터링 크론이 참조)"""
    state = {
        "goal": goal,
        "dod": dod,
        "task_id": task_id,
        "max_sprints": max_sprints,
        "sprint": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running"
    }
    state_path = NOVA_HOME/"logs"/"roop_state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"  ROOP 상태 저장: {state_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("goal", help="달성할 목표")
    parser.add_argument("--sprints", type=int, default=0,
                        help="최대 스프린트 수 (기본값 0 = 무제한, KPI_PASS까지 계속)")
    args = parser.parse_args()
    
    goal = args.goal
    max_sprints = args.sprints  # 0 = 무제한
    
    print("=" * 60)
    print(f"[ROOP] 시작")
    print(f"목표: {goal}")
    if max_sprints == 0:
        print(f"최대 스프린트: 무제한 (KPI_PASS까지 자율 반복)")
    else:
        print(f"최대 스프린트: {max_sprints}회")
    print("=" * 60)
    
    # 1-a. 시스템 자가 감사 (nova_self_audit.py) — 치명 문제 사전 차단
    print("\n[0/5] NOVA 시스템 자가 감사...")
    audit_py = HERMES_HOME / "bin" / "nova_self_audit.py"
    if audit_py.exists():
        r_audit = subprocess.run(
            [sys.executable, str(audit_py), "--quick"],  # 빠른 점검 (3초)
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HERMES_HOME": str(HERMES_HOME),
                 "NOVA_HOME": str(NOVA_HOME),
                 "PYTHONPATH": str(HERMES_HOME/"bin")+":"+str(Path.home()/"nova")}
        )
        if r_audit.returncode != 0:
            print("  ⛔ 자가 감사 실패 — 아래 항목 수정 후 재시도:")
            for line in r_audit.stdout.splitlines():
                if "✗" in line or "CRITICAL" in line or "HIGH" in line:
                    print(f"    {line.strip()}")
            print("\n  전체 감사: python3 ~/.hermes/bin/nova_self_audit.py")
            sys.exit(1)
        print("  ✅ 자가 감사 통과")

    # 1. DoD 자동 설계
    dod = classify_goal(goal)
    print("\n[1/5] DoD 자동 설계:")
    for c in dod["criteria"]:
        print(f"  ✓ {c}")
    print(f"  KPI 통과 키워드: {dod['pass_keyword']}")

    # 1-b. 잔여 ready 태스크 정리 (이전 세션 오염 방지)
    n_archived = archive_stale_ready_tasks()
    if n_archived:
        print(f"\n  [정리] 이전 세션 잔여 태스크 {n_archived}개 archive 완료")
    
    # 2. kpi_evaluate harness 프롬프트 업데이트
    print("\n[2/5] KPI 평가 기준 설정...")
    write_kpi_prompt(goal, dod)

    # 2-b. 공유 지식베이스에 스프린트 목표/KPI 초기화 (모든 에이전트 공유)
    try:
        import importlib.util as _ilu
        _path = HERMES_HOME / "bin" / "nova_shared_kb.py"
        if _path.exists():
            spec = _ilu.spec_from_file_location("nova_shared_kb", _path)
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.init_sprint(goal, dod["criteria"])
            print(f"  공유 KB 스프린트 초기화 완료: {_path}")
    except Exception as e:
        print(f"  WARN: 공유 KB 초기화 실패 (무시): {e}")

    # 3. kanban 태스크 등록
    print("\n[3/5] kanban nova-loop 태스크 등록...")
    task_id = register_kanban_task(goal, dod)
    if task_id:
        print(f"  태스크 생성: {task_id}")
    else:
        print("  WARNING: 태스크 생성 실패 (kanban 보드 확인 필요)")
    
    # 4. ROOP 상태 저장
    print("\n[4/5] ROOP 상태 저장...")
    save_roop_state(goal, dod, task_id, max_sprints)

    # 4-b. roop_monitor 크론잡 자동 등록 (5분마다 KPI_PASS 감지)
    monitor_py = HERMES_HOME / "scripts" / "roop_monitor.py"
    if monitor_py.exists():
        try:
            r_cron = subprocess.run(
                ["hermes", "cron", "create",
                 "--name", "roop-monitor",
                 "--schedule", "every 5m",
                 "--no-agent",
                 "--script", "roop_monitor.py"],
                capture_output=True, text=True
            )
            if r_cron.returncode == 0:
                print(f"  roop-monitor 크론잡 등록됨 (5분마다 KPI_PASS 감지)")
            else:
                print(f"  WARN: 크론잡 등록 실패 — 수동 확인 필요: {r_cron.stderr[:60]}")
        except Exception as e:
            print(f"  WARN: 크론잡 등록 실패 (무시): {e}")
    
    # 5. brain_watcher nudge → chain_engine 즉시 실행
    print("\n[5/5] NOVA 자율루프 트리거...")
    nudge_brain_watcher(goal)
    
    print("\n" + "=" * 60)
    print("[ROOP] 루프 시작됨!")
    print(f"현재: nova-autoplan 단계 (1/14)")
    print(f"진행 확인: hermes kanban --board nova-loop list")
    print(f"로그 확인: tail -f ~/.nova/logs/brain_watcher.log")
    print(f"KPI_PASS 시 자동 완료")
    print("=" * 60)

if __name__ == "__main__":
    main()
