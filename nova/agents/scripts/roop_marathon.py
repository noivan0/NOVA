#!/usr/bin/env python3
"""
roop_marathon.py — NOVA 자율화 완전 마라톤
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
현재 /roop 완료 대기 → 결과 분석 → 개선점 자가 발굴 → 다음 /roop 자동 시작
이 과정을 min_rounds회 반복하며 NOVA 에이전트 시스템을 완성한다.

사용:
  python3 ~/.hermes/bin/roop_marathon.py --rounds 10
  python3 ~/.hermes/bin/roop_marathon.py --rounds 10 --wait-current
"""
import os, sys, json, time, sqlite3, subprocess, argparse, uuid
from pathlib import Path
from datetime import datetime, timezone

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
NOVA   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))
BOARD  = "nova-loop"

ROOP_RUNNER = HERMES / "bin" / "roop_runner.py"
SELF_AUDIT  = HERMES / "bin" / "nova_self_audit.py"
CHAIN_PY    = NOVA   / "engines" / "chain.py"
LOG_FILE    = NOVA   / "logs" / "roop_marathon.log"
STATE_FILE  = NOVA   / "logs" / "marathon_state.json"

PYTHON3 = "/usr/bin/python3"
env = {
    **os.environ,
    "HERMES_HOME": str(HERMES),
    "NOVA_HOME":   str(NOVA),
    "PYTHONPATH":  str(HERMES/"bin")+":"+str(Path.home()/"nova"),
    "PATH":        str(Path.home()/".local"/"bin")+":"+os.environ.get("PATH",""),
}

# ── 마라톤 목표 시퀀스 ──────────────────────────────────────────────────────
# 각 라운드마다 점진적으로 더 복잡한 목표를 부여해 에이전트가 실질 작업을 수행하도록 함
MARATHON_GOALS = [
    # ─── 코어 기능 검증 (R1~R10) ───────────────────────────────────────────
    "NOVA research harness가 DDG 웹 검색을 수행하고 KB에 저장하는지 검증 — "
    "research_pass 키워드를 포함한 report.md 생성 및 brain.db takes 증가 확인",

    "nova-dev code_implement harness의 dod_verify phase가 py_compile + IMPORT OK를 "
    "실제로 output 변수에 담아 report.md에 저장하고 DoD 게이트를 통과하는지 검증",

    "nova-evaluator FORK 후 nova-retro + nova-learn이 병렬 실행(독립 PID)되고 "
    "CHAIN_JOIN nova-document-release로 합류하는지 검증",

    "NOVA 공유 지식베이스 성장 검증 — takes 300개 이상, pages 125개 이상, "
    "fact/pattern/lesson 타입 takes 비율 15% 이상 달성",

    "nova-dev DoD 미달 시 역방향 점프(nova-investigate)가 발동하고 "
    "investigate 완료 후 nova-dev가 재시작되는 전체 복구 체인 검증",

    # ─── 자율화 심화 (R6~R15) ─────────────────────────────────────────────
    "nova-sysaudit(system_audit harness)가 체인에서 정상 실행되고 "
    "27/27 자가감사 통과 후 nova-autoplan으로 재진입하는지 검증",

    "brain_watcher STARTUP 트리거가 done>0 AND active=0 상태에서 "
    "chain_engine을 즉시 실행하는지 실측 — [STARTUP] 로그 확인",

    "PARALLEL_GROUPS {nova-retro, nova-learn} 동시 파견이 orchestrator.log에 "
    "병렬 파견 로그로 확인되는지 검증 — 순차 아닌 동시 실행 증명",

    "nova-evaluator KPI_PASS 판정 시 nova-sysaudit → nova-autoplan 직전에 "
    "chain_engine이 루프를 차단하고 정상 종료하는지 전체 흐름 검증",

    "NOVA 에이전트 14단계 체인 완주 — autoplan→dev→review→cso→qa→ship→"
    "checkpoint→canary/health→evaluator→retro/learn→document→doc-release→sysaudit "
    "전체 실행 및 각 단계 DoD 통과 확인",

    # ─── 안정성 & 성능 (R11~R20) ──────────────────────────────────────────
    "brain.db WAL 동시 6-connection 읽기 안정성 + handoff.json atomic rename "
    "병렬 에이전트 충돌 없음 + write_progress fcntl LOCK_EX 정상 동작 검증",

    "NOVA 오케스트레이터 최적화 — code_implement + investigate + document_gen "
    "3개 에이전트 AGENT_TIMEOUT=600s 내 동시 완료 검증",

    "brain.db score_depth 개선 — learn harness 실행 후 fact/pattern 타입 "
    "takes가 증가하는지 확인. 목표: score_depth 60+ 달성",

    "nova-chain PATH ~/.local/bin 정상 주입 — hermes 명령이 chain_engine 내부에서 "
    "FATAL 없이 실행되는지 검증. [WARN] 태스크 목록 빈 값 없음 확인",

    "kanban.db WAL 무결성 — 100회 연속 실행 중 integrity_check = ok 유지 "
    "corrupt 발생 시 REINDEX 자동 복구 검증",

    # ─── KB & 지식 성장 (R16~R25) ─────────────────────────────────────────
    "research harness DDG 실검색 + KB BM25 조합으로 synthesis LLM에 "
    "kb_context가 주입되는지 확인 — synthesis.txt {{kb_context}} 치환 검증",

    "nova_shared_kb read_context 6가지 소스(sprint→KB→wiki→handoff→MEMORY→audit) "
    "모두 정상 반환 + max_chars=3000 예산 준수 확인",

    "wiki 63개+ 페이지 유지 — brain_watcher synthesize 엔진이 새 takes를 "
    "wiki markdown으로 변환하는지 검증. wiki 파일 수 증가 확인",

    "nova-learn write_learn_summary LLM 출력에 'Knowledge' + 'learn' DoD 키워드 "
    "포함 + output 변수로 report.md 저장 + kanban result=있음 확인",

    "nova-retro generate_retro LLM 출력에 'What Went Well' + '회고' DoD 키워드 "
    "포함 + kanban complete result=있음 + chain 정방향 진행 확인",

    # ─── 에이전트 팀 완성도 (R21~R30) ────────────────────────────────────
    "nova-review code_review harness fanout 패턴 — security_check + quality_check "
    "병렬 실행 후 review_summary 순차 + report.md에 CRITICAL=0 HIGH=0 출력",

    "nova-cso OWASP 평가 + CRITICAL=0 DoD 키워드 출력 — security_sign_off harness "
    "owasp_sign_off LLM 응답에 필수 키워드 포함 확인",

    "nova-qa pytest 실행 경로(~/.nova/workspace/code_implement) + "
    "analyze_results LLM 분석 + passed/failed:0 DoD 키워드 출력 검증",

    "nova-checkpoint go_nogo fanout — security_gate + quality_gate 병렬 후 "
    "final_judgment GO/LGTM 출력 + dod_verify output 변수 방식 확인",

    "nova-ship + nova-canary + nova-health — NOVA_DEPLOY_CMD/CANARY_METRICS_CMD "
    "미설정 시 passthrough 경로로 DoD 키워드 자동 출력 검증",

    # ─── 루프 엔지니어링 심화 (R26~R40) ──────────────────────────────────
    "BACKWARD_JUMP: nova-review DoD 미달 시 nova-dev 역방향 생성 "
    "+ is_backward_allowed 정방향 차단 + detect_loop [역방향] 카운팅 확인",

    "CHAIN_FORK/JOIN: checkpoint→canary+health FORK 후 둘 다 done → "
    "evaluator JOIN 생성. done_assignees 기반 all() 조건 정상 동작 검증",

    "detect_loop: nova-dev가 LOOP_DEPTH_MAX=10회 역방향 반복 시 "
    "nova-investigate 강제 트리거 + [LOOP] 타이틀 생성 확인",

    "KPI_PASS 루프 종료 체계: nova-evaluator → kpi_report.md KPI_PASS → "
    "document_release ROOP_COMPLETE → sysaudit→autoplan chain_engine 차단 검증",

    "nova-document-release write_loop_summary → kpi_report.md KPI_PASS 감지 → "
    "ROOP_COMPLETE brain.db 이벤트 기록 → 루프 종료 전체 흐름 검증",

    "스프린트 재진입: nova-sysaudit AUDIT_PASS → nova-autoplan 재생성 → "
    "새 스프린트 정상 시작 + sprint_state.json 업데이트 확인",

    "max_sprints=0 무제한: roop_state.json sprint 카운터가 증가해도 "
    "루프 차단 안 됨 + KPI_PASS만 종료 조건 확인",

    "CHAIN_FAIL → nova-investigate 폴백: blocked 에이전트 처리 후 "
    "nova-investigate 5Whys RCA + fix_proposal.md 생성 확인",

    "record_chain_step: forward/backward/fork/join/passthrough 모든 분기에서 "
    "brain.db takes에 chain 스텝 기록 확인 + TOCTOU 없음",

    "PASSTHROUGH: HARNESS_AGENTS 미등록 에이전트 ready 시 chain_engine이 "
    "자동 complete 처리 + CHAIN_DONE 다음 에이전트 정상 생성 확인",

    # ─── 시스템 자가진화 (R36~R50) ────────────────────────────────────────
    "nova-sysaudit sprint_directive.md 생성 → nova_shared_kb read_context ⑥에서 "
    "AUDIT_ISSUES 있을 때만 컨텍스트 주입 + nova-autoplan이 스프린트에 반영",

    "brain_watcher takes+5 → learn_engine 자동 트리거 + "
    "takes+15 → synthesize + takes+20 → research harness 실측 확인",

    "memory_slim 트리거: MEMORY.md 85%+ 시 slim 실행 + "
    "HERMES_HOME/memories/MEMORY.md 경로 brain_watcher/agent/shared_kb 3곳 일치",

    "orphan takes 자동 정리: brain.db orphan>=3 → fix_orphan 엔진 트리거 "
    "+ orphan 감소 확인",

    "KB → wiki 동기화: nova_kb_wiki_bridge Jaccard≥0.55 중복 감지 "
    "+ 새 takes → wiki 페이지 자동 생성 확인",

    # ─── 종합 완성도 검증 (R46~R60) ──────────────────────────────────────
    "NOVA 완전자율화 종합 R1: 14단계 체인 완주 + 자가감사 27/27 PASS + "
    "KB takes 300+ + 사람 개입 0 검증",

    "NOVA 완전자율화 종합 R2: 역방향 점프 3회 이내 복구 + KPI_PASS 루프 종료 "
    "+ nova-sysaudit AUDIT_PASS 순서로 정상 완주",

    "NOVA 완전자율화 종합 R3: 병렬 에이전트(retro+learn, canary+health) "
    "실제 독립 PID 확인 + orchestrator PARALLEL_GROUPS 동시 파견",

    "NOVA 완전자율화 종합 R4: brain.db health 88+ 달성 — "
    "score_overall 개선 + score_evolution 0.05+ 목표",

    "NOVA 완전자율화 종합 R5: pages 130+ + wiki 70개+ + "
    "takes types 다양성(insight/fact/pattern/lesson 각 5%+)",

    # ─── 마지막 50회: 반복 안정성 (R51~R100) ─────────────────────────────
    "NOVA 안정성 검증 — 50회 연속 마라톤 후에도 watcher 3× RUNNING "
    "+ kanban.db integrity_check=ok + brain.db WAL 정상",

    "NOVA 에이전트 평균 실행시간 측정 — nova-autoplan(research) 목표 60초이내 "
    "+ nova-dev(code_implement) 목표 90초이내 + nova-investigate 목표 90초이내",

    "NOVA KB 지식 순환 검증 — research → brain.db → wiki → shared_kb → "
    "다음 에이전트 컨텍스트 주입 전체 순환 경로 확인",

    "NOVA 에이전트 협력 검증 — handoff.json에 이전 에이전트 결과가 "
    "다음 에이전트 read_context에 반영되는지 확인",

    "NOVA 자율 개선 검증 — nova-sysaudit이 이슈 발견 시 sprint_directive.md "
    "생성 → 다음 nova-dev가 실제 수정 코드를 작성하는지 확인",
]

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}][MARATHON] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"round": 0, "completed": [], "results": [], "started_at": datetime.now(timezone.utc).isoformat()}

def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)

def get_kanban_summary() -> dict:
    """kanban.db 상태 요약"""
    db_path = HERMES / "kanban/boards" / BOARD / "kanban.db"
    if not db_path.exists():
        return {}
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        rows = db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status").fetchall()
        db.close()
        return dict(rows)
    except: return {}

def get_brain_summary() -> dict:
    """brain.db 상태 요약"""
    db_path = NOVA / "brain.db"
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        h = db.execute("SELECT score_overall,score_coverage,score_depth FROM brain_health ORDER BY rowid DESC LIMIT 1").fetchone()
        t = db.execute("SELECT COUNT(*) FROM takes").fetchone()[0]
        p = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        db.close()
        return {"health": h, "takes": t, "pages": p}
    except: return {}

def run_self_audit_quick() -> tuple[int, int]:
    """자가 감사 실행 → (pass, fail)"""
    r = subprocess.run([PYTHON3, str(SELF_AUDIT), "--quick"],
        capture_output=True, text=True, timeout=30, env=env)
    out = r.stdout
    for line in out.splitlines():
        if "PASS:" in line and "FAIL:" in line:
            parts = line.split()
            try:
                pass_n = int(parts[parts.index("PASS:")+1])
                fail_n = int(parts[parts.index("FAIL:")+1])
                return pass_n, fail_n
            except: pass
    return 0, 1

def _recover_stuck_loop(env: dict) -> bool:
    """active=1이 10분+ 고착 시 orchestrator 강제 실행으로 복구."""
    log("  [RECOVER] active=1 고착 감지 → orchestrator 강제 실행")
    # kanban.db에서 done 태스크 대량 archive (done 100+ → 성능 저하)
    try:
        db_path = HERMES / "kanban/boards" / BOARD / "kanban.db"
        db = sqlite3.connect(str(db_path), timeout=10)
        done_cnt = db.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
        if done_cnt > 80:
            # 오래된 done 태스크 archive
            old_done = db.execute(
                "SELECT id FROM tasks WHERE status='done' ORDER BY created_at ASC LIMIT ?",
                (done_cnt - 40,)
            ).fetchall()
            for (tid,) in old_done:
                subprocess.run(["hermes", "kanban", "--board", BOARD, "archive", tid],
                    capture_output=True, timeout=5, env=env)
            log(f"  [RECOVER] done {done_cnt}개 → {done_cnt-len(old_done)}개 archive 완료")
            db.close()
    except Exception as e:
        log(f"  [RECOVER] archive 오류: {e}")

    # flock 해제
    lock_p = Path("/tmp/nova_chain.lock")
    if lock_p.exists():
        lock_p.unlink(missing_ok=True)

    # orchestrator 직접 파견
    proc = subprocess.Popen(
        [PYTHON3, str(HERMES/"bin/nova_orchestrator.py"),
         "--board", BOARD, "--dispatch", "--wait"],
        env=env
    )
    log(f"  [RECOVER] orchestrator PID={proc.pid} 파견")
    try:
        proc.wait(timeout=180)
        log(f"  [RECOVER] orchestrator 완료 (exit={proc.returncode})")
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        log("  [RECOVER] orchestrator 180s timeout → kill")
        return False


def wait_for_current_roop(timeout_min: int = 60) -> bool:
    """현재 실행 중인 /roop 완료 대기"""
    log("현재 /roop 완료 대기 중...")
    deadline    = time.time() + timeout_min * 60
    last_log    = 0
    last_active = None
    stuck_since = None          # active=1 고착 시작 시각

    while time.time() < deadline:
        kanban = get_kanban_summary()
        active = kanban.get("running", 0) + kanban.get("ready", 0) + kanban.get("todo", 0)
        done   = kanban.get("done", 0)
        blocked = kanban.get("blocked", 0)

        if time.time() - last_log > 30:
            log(f"  kanban: active={active} done={done} blocked={blocked}")
            last_log = time.time()

        # 고착 감지: active=1 이 10분+ 지속 (에이전트 없는데 ready만 있는 경우)
        if active == 1:
            if stuck_since is None:
                stuck_since = time.time()
            elif time.time() - stuck_since > 600:          # 10분 고착
                log(f"  [WARN] active=1 10분+ 고착 → 자동 복구 시도")
                _recover_stuck_loop(env)
                stuck_since = None                          # 복구 후 리셋
        else:
            stuck_since = None                              # active 변화 시 리셋

        # 완료 조건: active(ready/running/todo)=0 이면 현재 /roop 완료
        # blocked는 실패/중단 태스크 — 무시하고 진행
        if active == 0:
            kpi_rpt = NOVA / "workspace" / "kpi_evaluate" / "kpi_report.md"
            if kpi_rpt.exists() and "KPI_PASS" in kpi_rpt.read_text(errors="replace"):
                log("  KPI_PASS 감지 → 루프 완료")
                return True
            # chain_engine flock 확인: 파일 존재만이 아닌 실제 잠금 여부 체크
            def _flock_held(path: str) -> bool:
                """flock 파일이 실제로 잠겨있는지 확인 (holder 없으면 False)"""
                import fcntl
                try:
                    fd = open(path, "w")
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                    return False  # 잠금 획득 가능 = holder 없음
                except OSError:
                    try: fd.close()
                    except: pass
                    return True   # 잠금 불가 = holder 있음
                except FileNotFoundError:
                    return False  # 파일 없음 = holder 없음

            lock_path = "/tmp/nova_chain.lock"
            chain_busy = _flock_held(lock_path) if Path(lock_path).exists() else False
            if not chain_busy:
                log(f"  active=0, done={done} → 현재 라운드 완료 처리")
                return True

        time.sleep(10)

    log(f"  타임아웃({timeout_min}분) — 강제 진행")
    return False

def archive_stale_tasks() -> int:
    """잔여 ready/blocked 태스크 archive + done 과다 시 정리 (Broken pipe 방지)"""
    r = subprocess.run(["hermes", "kanban", "--board", BOARD, "list", "--json"],
        capture_output=True, text=True, timeout=10, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    try:
        tasks = json.loads(r.stdout)
        count = 0
        for t in tasks:
            if t.get("status") in ("ready", "todo", "blocked"):
                tid = t.get("id")
                if tid:
                    subprocess.run(["hermes", "kanban", "--board", BOARD, "archive", tid],
                        capture_output=True, timeout=5, env=env)
                    count += 1
        if count:
            log(f"  잔여 태스크 {count}개 archive")

        # done 태스크 과다 시 정리 (80개 초과 → chain_engine Broken pipe 방지)
        done_tasks = [t for t in tasks if t.get("status") == "done"]
        if len(done_tasks) > 60:
            old_done = sorted(done_tasks, key=lambda x: x.get("created_at",""))[:len(done_tasks)-30]
            for t in old_done:
                subprocess.run(["hermes", "kanban", "--board", BOARD, "archive", t["id"]],
                    capture_output=True, timeout=5, env=env)
            log(f"  done 태스크 {len(done_tasks)}→{len(done_tasks)-len(old_done)}개 정리 (Broken pipe 방지)")
            count += len(old_done)
        return count
    except Exception as e:
        log(f"  archive 오류: {e}")
        return 0

def nudge_chain_engine() -> None:
    """brain.db에 nudge → chain_engine 트리거"""
    try:
        db = sqlite3.connect(str(NOVA/"brain.db"), timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT OR IGNORE INTO hermes_events (id,event_type,severity,title,detail,source_agent,is_read,created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (eid,"MARATHON_NUDGE","info","[MARATHON] roop 시작 nudge","auto",0,0,now))
        db.commit()
        time.sleep(2)
        db.execute("DELETE FROM hermes_events WHERE id=?", (eid,))
        db.commit(); db.close()
    except: pass

def run_roop(goal: str, round_n: int) -> bool:
    """roop_runner.py 실행"""
    log(f"\n{'═'*60}")
    log(f"  라운드 {round_n} 시작")
    log(f"  목표: {goal[:80]}...")
    log('═'*60)

    # 1. 잔여 태스크 정리
    archive_stale_tasks()
    time.sleep(2)

    # 2. roop_runner 실행
    r = subprocess.run([PYTHON3, str(ROOP_RUNNER), goal],
        capture_output=True, text=True, timeout=120, env=env)

    if r.returncode != 0:
        log(f"  roop_runner 실패: {r.stderr[:200]}")
        return False

    log(f"  roop_runner 시작 완료")
    nudge_chain_engine()
    return True

def run_marathon(min_rounds: int, wait_current: bool) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log(f"NOVA 자율화 마라톤 시작 — 목표: {min_rounds}회 완주")

    state = load_state()
    completed = state.get("round", 0)
    log(f"  이미 완료: {completed}회, 목표: {min_rounds}회")

    # 현재 실행 중인 /roop 완료 대기
    if wait_current:
        log("현재 실행 중인 /roop 완료 대기...")
        wait_for_current_roop(timeout_min=120)

    while completed < min_rounds:
        round_n = completed + 1
        goal_idx = (completed % len(MARATHON_GOALS))
        goal = MARATHON_GOALS[goal_idx]

        # 자가 감사
        log(f"\n[라운드 {round_n}] 시작 전 자가 감사...")
        pass_n, fail_n = run_self_audit_quick()
        log(f"  자가 감사: {pass_n} PASS / {fail_n} FAIL")
        if fail_n > 0:
            log(f"  ⚠ FAIL {fail_n}건 — 계속 진행 (nova-sysaudit이 수정할 것)")

        # brain 상태
        brain = get_brain_summary()
        log(f"  brain.db: health={brain.get('health')} takes={brain.get('takes')} pages={brain.get('pages')}")

        # roop 시작 (최대 3회 재시도)
        ok = False
        for attempt in range(3):
            ok = run_roop(goal, round_n)
            if ok:
                break
            log(f"  시작 실패 ({attempt+1}/3) — 20초 후 재시도")
            time.sleep(20)
        if not ok:
            log(f"  라운드 {round_n} 3회 시도 모두 실패 — 다음 라운드로")
            completed = round_n  # skip this round
            state["round"] = round_n
            save_state(state)
            time.sleep(10)
            continue

        # 완료 대기 (120분 — 복잡한 라운드 대비)
        finished = wait_for_current_roop(timeout_min=120)

        # 결과 기록
        brain_after = get_brain_summary()
        result = {
            "round":     round_n,
            "goal":      goal[:120],
            "audit":     {"pass": pass_n, "fail": fail_n},
            "brain_before": brain,
            "brain_after":  brain_after,
            "finished":  finished,
            "at":        datetime.now(timezone.utc).isoformat(),
        }
        state["results"].append(result)
        state["round"] = round_n
        save_state(state)

        log(f"\n[라운드 {round_n}] {'완료 ✅' if finished else '타임아웃 ⏱'}")
        log(f"  brain: takes {brain.get('takes')}→{brain_after.get('takes')} "
            f"pages {brain.get('pages')}→{brain_after.get('pages')}")

        completed = round_n
        time.sleep(10)

    log(f"\n{'━'*60}")
    log(f"마라톤 완료: {completed}/{min_rounds}회")
    log(f"결과: {STATE_FILE}")
    log('━'*60)

def main() -> None:
    p = argparse.ArgumentParser(description="NOVA 자율화 마라톤")
    p.add_argument("--rounds",        type=int, default=10, help="반복 횟수 (기본 10)")
    p.add_argument("--wait-current",  action="store_true",  help="현재 /roop 완료 대기 후 시작")
    args = p.parse_args()

    try:
        run_marathon(args.rounds, args.wait_current)
    except KeyboardInterrupt:
        log("\n사용자 중단 (Ctrl+C)")
        state = load_state()
        log(f"완료된 라운드: {state.get('round', 0)}/{args.rounds}")

if __name__ == "__main__":
    main()
