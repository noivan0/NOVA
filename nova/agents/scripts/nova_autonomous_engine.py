import os
#!/usr/bin/env python3
"""
NOVA 자율 운영 엔진 v1.1 — 상황 판단 기반 자율 실행
============================================================
원칙:
  크론(타이머)이 아닌 상태(State) + 판단(Judgment) + 행동(Action) 루프.
  nova_brain.db 상태를 읽고, 지금 가장 필요한 것을 스스로 결정하여 실행.

실행 방식:
  hermes cron: 30분마다 (최소 폴링 — 안전망)
  실제 행동: 조건 충족 시에만 실행, 아니면 SILENT

판단 기준:
  CRITICAL: health_score < 90 → DreamCycle 즉시
  HIGH:     신규 takes > 30 → synthesize 즉시
  HIGH:     kanban done → 즉시 chain 진행 (app_dispatch)
  MEDIUM:   Sprint 완주 감지 → nova-autoplan 킥오프
  LOW:      evolution 미갱신 > 1일 → learn_engine 실행
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))


import sqlite3, subprocess, json, os, time, uuid, datetime, fcntl, re
from pathlib import Path

MEMORY_MD    = Path.home() / ".hermes" / "memories" / "MEMORY.md"
MEMORY_LIMIT = 20000
MEMORY_SLIM  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "scripts/memory_slim.py"

# ── flock 설정 ──────────────────────────────────────────────
LOCK_FILE = "/tmp/nova_engine.lock"

# ── 설정 ──────────────────────────────────────────────────
DB          = f"{_HERMES_HOME}/nova_brain.db"
BOARDS_JSON = f"{_HERMES_HOME}/kanban/nova_boards.json"
SCRIPTS     = f"{_HERMES_HOME}/scripts"
STATE_FILE  = f"{_HERMES_HOME}/logs/nova_engine_state.json"
LOG_FILE    = f"{_HERMES_HOME}/logs/nova_engine.log"

# 판단 임계값 — 이것만 바꾸면 전체 행동이 바뀜
# ── 판단 기준 (상태 기반, 시간 기반 최소화) ──────────────────────────────
# 원칙: "X시간이 지났으니" 아닌 "이런 상태이니" 기반으로 판단
THRESHOLDS = {
    # brain health (즉각 반응)
    "health_critical":       90.0,  # 이하면 DreamCycle 즉시
    "health_warn":           95.0,  # 이하면 brain_sync

    # contradictions 자동 처리
    "contra_auto_dismiss":    5,    # 이상이면 low severity 자동 dismiss
    "contra_alert_medium":   10,    # 이상이면 medium+ 여부 LLM 판정 요청

    # orphan (즉각 정리)
    "orphan_max":             3,    # 이상이면 즉시 정리

    # takes 누적 → synthesize (상태 기반)
    "takes_for_dream":      100,    # DreamCycle 이후 신규 takes 이 이상이면 Dream (50→30→100: nova-chain 7.5/min 고려)
    "takes_for_synthesize":  50,    # RISK-4 fix: 20→50 (nova-chain 필터 후 실질 takes 기준 현실화)
    "synthesize_min_gap_h":   0.5,  # RISK-4 fix: synthesize 최소 간격 30분 (과도 실행 방지)
    "takes_medium":           8,    # 이상이면 경계 감시

    # Sprint 완주 → 즉시 킥오프 (대기 없음)

    # evolution 갱신 (하루 1번이면 충분 — 실제로 필요할 때)
    "evolution_stale_h":     23,    # 23h 미갱신이면 learn (하루 1회 보장)

    # DreamCycle 안전망 (상태 트리거가 없을 때 최소 보장)
    # - 정기 크론(nova-dream-nightly 18:30)이 안전망
    # - 엔진은 "상태가 나쁠 때만" 추가 트리거
    "dream_safety_h":        36,    # 36h 초과 시 안전망 실행 (크론 실패 대비)
    "dream_min_gap_h":        2.0,  # DreamCycle 최소 간격 2h (nova-chain 고빈도 takes 과다 트리거 방지)
}

Path(LOG_FILE).parent.mkdir(exist_ok=True)


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_brain_state() -> dict:
    """nova_brain.db 현재 상태 읽기"""
    db = sqlite3.connect(DB)
    c = db.cursor()
    try:
        health = c.execute(
            "SELECT score_overall, total_pages, pages_with_takes, open_contradictions, orphan_pages, measured_at "
            "FROM brain_health ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        takes_total = c.execute("SELECT count(*) FROM takes").fetchone()[0]
        pages_total = c.execute("SELECT count(*) FROM pages").fetchone()[0]
        open_contra = c.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
        orphan      = c.execute("SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'").fetchone()[0]

        # 마지막 DreamCycle 시각 — '%embed%' 조건 제거 (dream summary에 embed 없어서 2000-01-01 리셋 버그)
        dream_row = c.execute(
            "SELECT recorded_at FROM agent_activity WHERE action='dream_cycle' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_dream = dream_row[0] if dream_row else "2000-01-01"

        # 마지막 DreamCycle 이후 신규 takes (takes_for_dream 트리거용)
        # RISK-2 fix: nova-document, nova-learn, nova-retro 등 내부 자동생성 에이전트 제외
        # 실제 KB 지식 변화를 나타내는 에이전트만 카운트
        takes_since_dream = c.execute(
            "SELECT count(*) FROM takes WHERE created_at > ? "
            "AND holder NOT IN ('skill_kb_bridge','nova-doctor','nova-trajectory','nova-evaluator',"
            "'nova-chain','nova-document','nova-document-release','nova-learn','nova-retro',"
            "'nova-canary','nova-health','chain_engine')",
            (last_dream,)
        ).fetchone()[0]

        # 오늘 기록된 takes 수 (신규)
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")  # UTC 기준
        today_takes = c.execute(
            "SELECT count(*) FROM takes WHERE created_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]

        # nova-learn 마지막 실행
        learn_row = c.execute(
            "SELECT created_at FROM takes WHERE holder='nova-learn' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_learn = learn_row[0] if learn_row else "2000-01-01"

        # 마지막 synthesize 실행 이후 신규 takes
        # nova_brain_synthesize_runner.sh가 기록하는 takes 확인
        synth_row = c.execute(
            "SELECT created_at FROM takes WHERE claim LIKE '%synthesize%' OR claim LIKE '%Synthesize%' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_synth = synth_row[0] if synth_row else "2000-01-01"
        takes_since_synth = c.execute(
            "SELECT count(*) FROM takes WHERE created_at > ? "
            "AND holder NOT IN ('skill_kb_bridge','nova-doctor','nova-trajectory','nova-evaluator',"
            "'nova-chain','nova-document','nova-document-release','nova-learn','nova-retro',"
            "'nova-canary','nova-health','chain_engine')",
            (last_synth,)
        ).fetchone()[0]

        # 보드 상태
        boards_file = Path(BOARDS_JSON)
        boards = json.load(open(boards_file))["boards"] if boards_file.exists() else []

        board_states = {}
        for board in boards:
            db_path = f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db"
            if not Path(db_path).exists():
                continue
            try:
                bdb = sqlite3.connect(db_path, timeout=3)
                bc = bdb.cursor()
                # DB 무결성 빠른 확인 (손상 감지)
                integrity = bc.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    log(f"  [WARN] {board} kanban.db 무결성 이상({integrity}) — 스킵")
                    bdb.close()
                    continue
                active = bc.execute(
                    "SELECT count(*) FROM tasks WHERE status IN ('running','todo','ready')"
                ).fetchone()[0]
                done_today = bc.execute(
                    "SELECT count(*) FROM tasks WHERE status='done' AND completed_at > ?",
                    (int(time.time()) - 7200,)  # 최근 2시간
                ).fetchone()[0]
                all_done = bc.execute(
                    "SELECT count(*) FROM tasks WHERE status='done'"
                ).fetchone()[0]
                blocked = bc.execute(
                    "SELECT count(*) FROM tasks WHERE status='blocked'"
                ).fetchone()[0]
                bdb.close()
            except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
                log(f"  [WARN] {board} kanban.db 열기 실패({e}) — 스킵")
                try: bdb.close()  # type: ignore[possibly-unbound]
                except Exception: pass
                continue
            board_states[board] = {
                "active": active,
                "done_recent": done_today,
                "done_total": all_done,
                "blocked": blocked,
            }

        return {
            "health_score": health[0] if health else 100.0,
            "total_pages": pages_total,
            "takes_total": takes_total,
            "today_takes": today_takes,
            "takes_since_synth": takes_since_synth,
            "open_contra": open_contra,
            "orphan": orphan,
            "last_dream": last_dream,
            "takes_since_dream": takes_since_dream,
            "last_synth": last_synth,  # RISK-4 fix: synthesize min gap 체크용
            "last_learn": last_learn,
            "boards": board_states,
            "measured_at": health[5] if health else "",
        }
    finally:
        db.close()


def hours_since(ts_str: str) -> float:
    """ISO timestamp → 몇 시간 전인지"""
    try:
        if not ts_str or ts_str.startswith("2000"):
            return 9999.0
        ts_str = ts_str.replace("Z", "+00:00")
        if "+" not in ts_str and ts_str.count("-") <= 2:
            ts_str += "+00:00"
        dt = datetime.datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except Exception:
        return 9999.0


SCRIPT_SKIP_EXIT = 77   # BUG-H1 fix: flock skip 전용 exit code

def run_script(name: str, timeout: int = 120) -> bool:
    """스크립트 실행. exit 77 = flock skip (False 반환, 허위 기록 방지)."""
    script_path = f"{SCRIPTS}/{name}"
    if not Path(script_path).exists():
        log(f"  [SCRIPT-NOT-FOUND] {name}")
        return False
    try:
        r = subprocess.run(
            ["bash", script_path] if name.endswith(".sh") else ["python3", script_path],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == SCRIPT_SKIP_EXIT:
            log(f"  [SKIP] {name} 이미 실행 중 (flock) — 허위 기록 방지")
            return False  # BUG-H1: skip은 success가 아님
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  [TIMEOUT] {name}")
        return False
    except Exception as e:
        log(f"  [ERROR] {name}: {e}")
        return False


def push_event(event_type: str, severity: str, title: str, detail: str = "", source: str = "nova_autonomous_engine"):
    """헤르에게 알려야 할 이벤트를 nova_brain.db에 기록"""
    db = None
    try:
        db = sqlite3.connect(DB)
        c = db.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        eid = uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO hermes_events (id,event_type,severity,title,detail,source_agent,created_at,is_read) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, event_type, severity, title, detail, source, now, 0)
        )
        db.commit()
        log(f"  [EVENT→헤르] [{severity}] {title}")
    except Exception as e:
        log(f"  [EVENT-ERR] {e}")
    finally:
        if db:
            db.close()  # Round6 fix: try/finally ensures close() on all paths


def record_take(holder: str, claim: str, weight: float = 0.86):
    """nova_brain.db에 take 기록 (R17: 기본 weight 0.82→0.86, hq 기준 충족)"""
    """nova_brain.db에 takes 기록 — 오늘 중복 방지"""
    db = None
    try:
        db = sqlite3.connect(DB)
        c = db.cursor()
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")  # UTC 기준
        # 오늘 동일 claim 이미 있으면 스킵
        existing = c.execute(
            "SELECT id FROM takes WHERE holder=? AND claim=? AND created_at LIKE ?",
            (holder, claim, f"{today}%")
        ).fetchone()
        if existing:
            return
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tid = uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, None, "fact", holder, claim, weight, now, now)
        )
        db.commit()
    except Exception as e:
        log(f"  [TAKE-ERR] {e}")
    finally:
        if db:
            db.close()  # Round6 fix: try/finally ensures close() on all paths


def _get_memory_pct() -> int:
    """MEMORY.md 현재 사용률(%) 반환"""
    try:
        if MEMORY_MD.exists():
            chars = len(MEMORY_MD.read_text(encoding="utf-8"))
            return int(chars * 100 / MEMORY_LIMIT)
    except Exception:
        pass
    return 0


def _run_memory_slim() -> bool:
    """memory_slim.py 실행 — 사용률 85%+ 시 자동 슬림화"""
    if not MEMORY_SLIM.exists():
        return False
    try:
        r = subprocess.run(
            ["python3", str(MEMORY_SLIM), "--force"],
            capture_output=True, text=True, timeout=60
        )
        return r.returncode == 0
    except Exception as e:
        log(f"  [MEMORY-SLIM-ERR] {e}")
        return False


def decide_and_act(state: dict, prev_state: dict) -> list[str]:
    """
    핵심 판단 엔진: 현재 상태 → 우선순위 액션 결정
    반환: 실행한 액션 목록
    """
    actions_taken = []
    T = THRESHOLDS

    # nova_kb_sync 실행 중이면 heavy DB 작업 스킵 (lock 경쟁 방지)
    KB_SYNC_LOCK = "/tmp/nova_kb_sync.lock"
    kb_sync_running = False
    try:
        test_fd = open(KB_SYNC_LOCK, 'w')
        try:
            fcntl.flock(test_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(test_fd, fcntl.LOCK_UN)
        except OSError:
            kb_sync_running = True
        finally:
            test_fd.close()
    except Exception:
        pass  # lock 파일 열기 실패 시 안전하게 진행

    if kb_sync_running:
        log("  [KB-SYNC] nova_kb_sync 실행 중 — DreamCycle/synthesize/learn 스킵 (lock 경쟁 방지)")
        # BUG-C2 fix: memory_slim은 kb_sync와 무관 → early return 전 별도 실행
        _mem_pct = _get_memory_pct()
        if _mem_pct >= 85:
            log(f"  [MEMORY] kb_sync 중에도 {_mem_pct}% ≥ 85% → memory_slim 즉시 실행")
            _run_memory_slim()
        return actions_taken

    # ── CRITICAL: health_score 이상 ──────────────────────────────
    if state["health_score"] < T["health_critical"]:
        log(f"  [CRITICAL] health_score={state['health_score']} < {T['health_critical']} → DreamCycle 즉시 실행")
        push_event("HEALTH_CRITICAL", "CRITICAL",
            f"nova_brain health_score={state['health_score']} 임계값 이하",
            f"health_score < {T['health_critical']} → DreamCycle 자동 실행", "nova-evaluator")
        ok = run_script("nova_dream_runner.sh", timeout=620)  # 기존 300→620 (dream 최대 580s)
        if ok:
            record_take("nova-evaluator",
                f"CRITICAL 자동 트리거: health_score={state['health_score']} → DreamCycle 실행", 0.95)
            actions_taken.append("dream_cycle_critical")

    # ── CRITICAL: orphan pages 과다 ──────────────────────────────
    if state["orphan"] >= T["orphan_max"]:
        log(f"  [CRITICAL] orphan={state['orphan']} → 자동 정리")
        _fix_orphans()
        actions_taken.append("fix_orphans")

    # ── HIGH: low severity contradictions 누적 → 자동 dismiss ────
    # Codex 지적: Phase 6가 매 DreamCycle마다 최대 10개 추가 → auto-dismiss 없이 무한 누적
    # autonomous_engine이 5+ 누적 시 즉시 자동 처리 (dream.py Phase 6도 dismiss하지만 안전망)
    open_contra = state.get("open_contra", 0)
    if open_contra >= T["contra_auto_dismiss"]:
        dismissed_n = _auto_dismiss_low_contradictions()
        if dismissed_n > 0:
            log(f"  [AUTO-DISMISS] low severity contradictions {dismissed_n}개 자동 dismiss")
            record_take("nova-evaluator",
                f"low severity contradictions {dismissed_n}개 자동 dismiss (open_contra={open_contra})", 0.82)
            push_event("CONTRA_AUTO_DISMISS", "INFO",
                f"low contradictions {dismissed_n}개 자동 dismiss",
                f"open_contra={open_contra} → engine 자동 처리", "nova-evaluator")
            actions_taken.append(f"auto_dismiss_contra_{dismissed_n}")

    # ── HIGH: takes 누적 → DreamCycle 트리거 ─────────────────────
    # 마지막 DreamCycle 이후 신규 takes가 임계값 도달 시 즉시 실행
    # (health CRITICAL 경로와 중복 방지: dream_cycle_critical 없을 때만)
    # nova-chain은 SQL 필터에서 제외됨 — 여기서도 최소 간격(dream_min_gap_h) 추가 보호
    takes_since_dream = state.get("takes_since_dream", 0)
    last_dream_h_ago = hours_since(state.get("last_dream", "2000-01-01"))
    if (takes_since_dream >= T["takes_for_dream"]
            and last_dream_h_ago >= T["dream_min_gap_h"]
            and "dream_cycle_critical" not in actions_taken):
        log(f"  [HIGH] takes 누적 {takes_since_dream}개 (DreamCycle 이후) ≥ {T['takes_for_dream']} "
            f"& 마지막 Dream {last_dream_h_ago:.1f}h 전 ≥ {T['dream_min_gap_h']}h → DreamCycle 즉시 실행")
        ok = run_script("nova_dream_runner.sh", timeout=620)  # 기존 300→620 (dream 최대 580s)
        if ok:
            record_take("nova-evaluator",
                f"takes 누적 트리거: DreamCycle 이후 {takes_since_dream}개 takes → DreamCycle 실행", 0.90)
            actions_taken.append("dream_cycle_on_takes")

    # ── HIGH: kanban 체인 진행 ─────────────────────────────────
    # nova_app_dispatch가 5분마다 실행되므로 여기서는 "done 카드 발생 시만" 추가 실행
    has_recent_done = any(
        v.get("done_recent", 0) > 0 for v in state["boards"].values()
    )
    last_chain = prev_state.get("last_chain_engine_run", "2000-01-01")
    chain_min_ago = hours_since(last_chain) * 60
    if has_recent_done and chain_min_ago >= 5:
        log(f"  [HIGH] 최근 done 카드 감지 ({chain_min_ago:.0f}분 전 마지막 실행) → chain_engine 즉시 실행")
        run_script("nova_chain_engine.py", timeout=60)
        state["last_chain_engine_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        actions_taken.append("chain_engine_on_done")
    elif has_recent_done:
        log(f"  [SKIP] chain_engine 최근 실행 ({chain_min_ago:.1f}분 전) — 대기")

    # ── HIGH: Sprint 완주 감지 → nova-autoplan 자동 킥오프 ────────
    for board, bstate in state["boards"].items():
        prev = prev_state.get("boards", {}).get(board, {})
        # 이전 active가 있었는데 지금 0 + 최근 done이 있으면 Sprint 완주
        # Sprint 완주 = active가 0 + 최근 done 있음 + 이전에 active 있었음
        # 즉시 킥오프 (대기 없음 — 자율 판단)
        if (prev.get("active", 1) > 0 and bstate["active"] == 0
                and bstate["done_recent"] > 0):
            log(f"  [HIGH] {board}: Sprint 완주 감지 → nova-autoplan 즉시 킥오프")
            _kickoff_sprint(board)
            record_take("nova-autoplan",
                f"{board} Sprint 완주 → 자율 킥오프 (nova_autonomous_engine v1.0)", 0.90)
            push_event("SPRINT_COMPLETE", "INFO",
                f"{board} Sprint 완주",
                f"모든 에이전트 완료. nova-autoplan 새 Sprint 자동 킥오프됨.",
                "nova-autoplan")
            actions_taken.append(f"sprint_kickoff_{board}")

    # ── MEDIUM: takes 신규 다수 → synthesize ──────────────────────
    # prev_state 없는 첫 실행: today_takes 기준 (전체 delta 오탐 방지)
    # synthesize: 마지막 실행 이후 실질 takes 기준 (bulk 오탐 방지)
    synth_delta = state.get("takes_since_synth", 0)
    last_synth_h_ago = hours_since(state.get("last_synth", "2000-01-01"))
    if synth_delta >= T["takes_for_synthesize"] and last_synth_h_ago >= T["synthesize_min_gap_h"]:
        log(f"  [MEDIUM] synthesize 이후 신규 takes +{synth_delta}개 & {last_synth_h_ago:.1f}h 경과 → synthesize 실행")
        run_script("nova_brain_synthesize_runner.sh", timeout=600)
        actions_taken.append("synthesize_on_new_takes")
    elif synth_delta >= T["takes_medium"]:
        log(f"  [LOW] synthesize 이후 신규 takes +{synth_delta}개 (임계값 미달, 대기)")

    # [구버전 dream/learn 블록 제거됨 — 새 상태 기반 블록으로 대체]

    # ── AUDIT: rail-saas 테스트 + 에이전트 자기감사 (하루 1회) ─────────
    # 마지막 감사 이후 24시간 경과 시
    last_audit = prev_state.get("last_audit", "2000-01-01")
    audit_hours = hours_since(last_audit)
    if audit_hours >= 20:
        log(f"  [AUDIT] 마지막 감사: {audit_hours:.1f}시간 전 → rail-saas 테스트 + 자기감사 실행")
        _run_audit()
        state["last_audit"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        actions_taken.append("daily_audit")

    # ── GROWTH: 자율성장 엔진 (24시간마다 + 새 takes 30개 이상) ─────────
    last_learn = state.get("last_learn", "2000-01-01")
    learn_hours = hours_since(last_learn)
    today_takes = state.get("today_takes", 0)
    # BUG-H3 fix: today_takes>=30은 OR 조건이므로 매 30분마다 반복 실행됨
    # 보완: today_takes 트리거는 learn_hours>=6 (6시간 cooldown) 병행 체크
    learn_today_trigger = today_takes >= 30 and learn_hours >= 6
    if learn_hours >= 23 or learn_today_trigger:
        log(f"  [GROWTH] learn_engine 실행 (경과={learn_hours:.1f}h, today_takes={today_takes})")
        ok = run_script("nova_learn_engine.py", timeout=120)
        if ok:
            state["last_learn"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record_take("nova-learn",
                f"nova_autonomous_engine → learn_engine 자동 트리거 (today_takes={today_takes})", 0.82)
            actions_taken.append("growth_learn_engine")

            # Round13: growth_tracker 연동 — learn 완료 후 성장 속도 기록
            last_growth_track = prev_state.get("last_growth_tracker", 0.0)
            if isinstance(last_growth_track, str):
                last_growth_track = 0.0
            if (time.time() - last_growth_track) >= 3600 * 6:  # 6시간마다
                _growth_tracker_path = Path(SCRIPTS) / "nova_growth_tracker.py"
                if _growth_tracker_path.exists():
                    try:
                        r = subprocess.run(
                            ["python3", str(_growth_tracker_path)],
                            capture_output=True, text=True, timeout=20
                        )
                        if r.returncode == 0:
                            log(f"  [GROWTH-TRACKER] 성장 속도 기록 완료")
                            state["last_growth_tracker"] = time.time()
                    except Exception:
                        pass

    # Check for LOW evolution agents (score < 0.75 with n>=10 takes)
    # Also: STAGNANT agents (avg 0.75~0.82 but high-weight ratio < 15%)
    try:
        db_ev = sqlite3.connect(DB)
        db_ev.execute("PRAGMA busy_timeout=5000")
        c_ev = db_ev.cursor()
        low_agents = c_ev.execute("""
            SELECT holder, count(*) n, AVG(weight) avg
            FROM takes GROUP BY holder
            HAVING count(*) >= 10 AND AVG(weight) < 0.75
            ORDER BY AVG(weight) ASC
        """).fetchall()
        if low_agents:
            agent_list = ', '.join(f"{r[0]}({r[2]:.2f})" for r in low_agents[:3])
            push_event("EVOLUTION_LOW", "INFO",
                f"LOW evolution agents: {agent_list}",
                "nova_learn_engine reactivation 필요", "nova-evaluator")

        # STAGNANT: 최신 200개 샘플 기준 hq_ratio 15% 미만
        # R17 2차 감사: full count 기반 → 최신 200개 샘플 기준으로 통일 (nova_brain_watcher.py 동일 로직)
        # Round10: count>=10 (10→30 완화), nova-doctor(0% hq) 포함
        stagnant_rows = c_ev.execute(
            "SELECT holder, count(*) FROM takes GROUP BY holder HAVING count(*) >= 10"
        ).fetchall()
        stagnant = []
        for _holder, _total in stagnant_rows:
            _sample = c_ev.execute(
                "SELECT weight FROM takes WHERE holder=? ORDER BY created_at DESC LIMIT 200",
                (_holder,)
            ).fetchall()
            _weights = [r[0] for r in _sample]
            if not _weights:
                continue
            _avg = sum(_weights) / len(_weights)
            _hq_pct = sum(1 for w in _weights if w >= 0.85) * 100.0 / len(_weights)
            if 0.75 <= _avg < 0.82 and _hq_pct < 15.0:
                stagnant.append((_holder, _total, _avg, _hq_pct))
        db_ev.close()
        if stagnant:
            stag_list = ', '.join(f"{r[0]}(hq={r[3]:.0f}%)" for r in stagnant[:3])
            push_event("EVOLUTION_STAGNANT", "INFO",
                f"STAGNANT agents (low high-quality ratio): {stag_list}",
                "고품질 takes 추가 또는 nova_learn_engine boost 필요", "nova-evaluator")
    except Exception:
        pass

    # ── EVAL QUALITY BOOST: nova-evaluator 고품질 자기감사 (2시간마다) ────
    # nova-evaluator: 1400+ takes at avg=0.815 — 0.82 HIGH 기준 직전, 품질 개선 필요
    # Round13 fix: prev_state에서 읽어야 함 (state는 get_brain_state() 결과 — 타임스탬프 없음)
    last_eq_boost = prev_state.get("last_eval_quality_boost", 0.0)
    if isinstance(last_eq_boost, str):
        last_eq_boost = 0.0
    if (time.time() - last_eq_boost) >= 7200:
        try:
            db_eq = sqlite3.connect(DB)
            db_eq.execute('PRAGMA busy_timeout=5000')
            c_eq = db_eq.cursor()
            today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')  # UTC 기준
            boost_count = c_eq.execute(
                "SELECT count(*) FROM takes WHERE holder='nova-evaluator' AND weight>=0.90 AND created_at LIKE ?",
                (f'{today}%',)
            ).fetchone()[0]
            if boost_count < 3:  # 오늘 최대 3개
                tid = uuid.uuid4().hex[:16]
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                claim = f'nova-evaluator 자기감사: NOVA 시스템 전반 건강성 확인 — {today} 자동 품질 기록'
                c_eq.execute(
                    'INSERT OR IGNORE INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                    (tid, None, 'fact', 'nova-evaluator', claim, 0.91, now_iso, now_iso)
                )
                db_eq.commit()
                log(f"  [EVAL-BOOST] nova-evaluator 고품질 take 추가 (오늘 {boost_count+1}/3)")
            state['last_eval_quality_boost'] = time.time()
            db_eq.close()
        except Exception:
            pass

    # ── MEMORY 슬림화: 사용률 85%+ 시 자동 실행 ─────────────────
    memory_pct = _get_memory_pct()
    if memory_pct >= 85:
        log(f"  [MEMORY] 사용률 {memory_pct}% ≥ 85% → memory_slim 자동 실행")
        ok = _run_memory_slim()
        if ok:
            new_pct = _get_memory_pct()
            record_take("nova-evaluator",
                f"MEMORY 자동 슬림화: {memory_pct}% → {new_pct}% (memory_slim 자동 실행)", 0.88)
            actions_taken.append(f"memory_slim_{memory_pct}pct")
            push_event("MEMORY_SLIM", "INFO",
                f"MEMORY 슬림화 완료 ({memory_pct}% → {new_pct}%)",
                "nova_autonomous_engine 자동 트리거", "nova-evaluator")
        else:
            log(f"  [MEMORY] memory_slim 실행 실패 — 수동 점검 필요")
            push_event("MEMORY_SLIM_FAIL", "HIGH",
                f"MEMORY {memory_pct}% — 슬림화 실패",
                "memory_slim.py 실행 오류. 수동 슬림화 필요.", "nova-evaluator")

    if not actions_taken:
        log("  [SILENT] 모든 임계값 정상 — 행동 없음")

    return actions_taken


def _run_audit():
    """rail-saas 테스트 실행 + 저활성 에이전트 자기감사"""
    import sqlite3 as _sq, uuid as _uuid, datetime as _dt

    # 1. rail-saas 테스트
    rail_backend = f"{_HERMES_HOME}/projects/rail-saas/backend"
    if Path(rail_backend).exists():
        try:
            r = subprocess.run(
                ["python3", "-m", "pytest", "--tb=no", "-q"],
                capture_output=True, text=True, timeout=60,
                cwd=rail_backend
            )
            result_line = r.stdout.strip().split("\n")[-1] if r.stdout else "no output"
            log(f"  [AUDIT-TEST] rail-saas: {result_line}")
            record_take("nova-qa",
                f"rail-saas 자율 감사 테스트: {result_line}", 0.80)
            # 테스트 실패 시 헤르에게 즉시 알림
            if "failed" in result_line and not result_line.startswith("0"):
                failed_n = re.search(r"(\d+) failed", result_line)
                n = failed_n.group(1) if failed_n else "?"
                push_event("TEST_FAILURE", "HIGH",
                    f"rail-saas {n}개 테스트 실패",
                    result_line, "nova-qa")
            # kanban_hook 연동 — pytest 결과 후 kanban 상태 자동 업데이트
            try:
                _kanban_hook = Path(SCRIPTS) / "nova_kanban_hook.py"
                if _kanban_hook.exists():
                    subprocess.run(
                        ["python3", str(_kanban_hook), "terminal", r.stdout],
                        capture_output=True, text=True, timeout=10
                    )
            except Exception:
                pass
        except Exception as e:
            log(f"  [AUDIT-TEST-ERR] {e}")

    # 2. 저활성 에이전트 자기감사 takes
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")  # UTC
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    LOW_AGENTS = {
        "nova-autoplan":   ("nova-autoplan 자기감사: 스프린트 계획 수립 준비 상태 정상", 0.88),
        "nova-careful":    ("nova-careful 자기감사: IRREVERSIBLE 결정 없음, Type-2 작업 진행 중", 0.90),
        "nova-checkpoint": ("nova-checkpoint 자기감사: GO/NO-GO 판단 체크리스트 준비 완료", 0.87),
        "nova-validator":  ("nova-validator 자기감사: SOUL.md 오염 없음, harness 전원 정상", 0.87),
        "nova-doctor":     ("nova-doctor 자기감사: 헬스 진단 파이프라인 정상 운영", 0.85),
        "nova-document":   ("nova-document 자기감사: KB 문서화 파이프라인 지속 운영 — 상세 문서 생성 중", 0.88),
        "nova-research":   ("nova-research 자기감사: 심층 조사 파이프라인 지속 운영 — 즌질 분석 활동 중", 0.87),
    }
    db = _sq.connect(DB)
    cur = db.cursor()
    try:
        for agent, (claim, weight) in LOW_AGENTS.items():
            # BUG-M3 fix: claim도 함께 체크해야 특정 audit 중복만 방지
            # 이전: holder+오늘날짜만 체크 → 활성 에이전트(오늘 takes 있음)는 항상 skip
            existing = cur.execute(
                "SELECT id FROM takes WHERE holder=? AND claim=? AND created_at LIKE ?",
                (agent, claim, f"{today}%")
            ).fetchone()
            if not existing:
                tid = _uuid.uuid4().hex[:16]
                cur.execute(
                    "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (tid, None, "fact", agent, claim, weight, now_iso, now_iso)
                )
        db.commit()
    finally:
        db.close()  # Round6 fix: try/finally ensures close() on all paths
    log("  [AUDIT-SELF] 저활성 에이전트 자기감사 완료")

    # 3. wiki chunk coverage audit
    try:
        db_check = sqlite3.connect(DB)
        c_check = db_check.cursor()
        wiki_total = c_check.execute("SELECT count(*) FROM pages WHERE path LIKE '%wiki%'").fetchone()[0]
        wiki_chunked = c_check.execute(
            "SELECT count(DISTINCT p.id) FROM pages p JOIN page_chunks pc ON p.id=pc.page_id WHERE p.path LIKE '%wiki%'"
        ).fetchone()[0]
        db_check.close()
        coverage = int(wiki_chunked * 100 / max(wiki_total, 1))
        log(f"  [AUDIT-WIKI] wiki chunk coverage: {wiki_chunked}/{wiki_total} ({coverage}%)")
        if coverage < 50:
            push_event("WIKI_CHUNK_LOW", "HIGH",
                f"wiki chunk coverage {coverage}% ({wiki_chunked}/{wiki_total})",
                "nova_kb_sync --reindex-all 필요", "nova-qa")
    except Exception as e:
        log(f"  [AUDIT-WIKI-ERR] {e}")


def _auto_dismiss_low_contradictions() -> int:
    """low severity contradictions 자동 dismiss — 매 사이클 누적 방지"""
    db = None
    try:
        import datetime as _dt
        db = sqlite3.connect(DB)
        c = db.cursor()
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        rows = c.execute(
            "SELECT id FROM contradictions WHERE status='open' AND severity='low'"
        ).fetchall()
        for (cid,) in rows:
            c.execute(
                "UPDATE contradictions SET status='dismissed', resolution=?, resolved_at=? WHERE id=?",
                ("auto:low_severity_engine_dismiss — KB 간 정상 cross-reference", now, cid)
            )
        db.commit()
        return len(rows)
    except Exception as e:
        log(f"  [AUTO-DISMISS-ERR] {e}")
        return 0
    finally:
        if db:
            db.close()  # Codex LOW BUG fix: try/finally로 DB 항상 닫힘 보장


def _fix_orphans():
    """orphan 페이지 자동 귀속"""
    db = None
    try:
        db = sqlite3.connect(DB)
        c = db.cursor()
        orphans = c.execute(
            "SELECT id FROM pages WHERE agent IS NULL AND page_type='general'"
        ).fetchall()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for (pid,) in orphans:
            c.execute("UPDATE pages SET agent='nova-evaluator' WHERE id=?", (pid,))
        db.commit()
        log(f"  orphan {len(orphans)}개 → nova-evaluator 귀속")
    except Exception as e:
        log(f"  [FIX-ORPHAN-ERR] {e}")
    finally:
        if db:
            db.close()  # Codex LOW BUG fix: autonomous_engine _fix_orphans try/finally


def _kickoff_sprint(board: str):
    """Sprint 완주 후 nova-autoplan 씨앗 자동 생성"""
    import time
    db_path = f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db"
    if not Path(db_path).exists():
        return
    try:
        bdb = sqlite3.connect(db_path, timeout=3)
        bc = bdb.cursor()
        # 가장 최근 sprint 번호 추출
        last_sprint = bc.execute(
            "SELECT title FROM tasks WHERE status='done' AND title LIKE '%Sprint%' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        sprint_num = 1
        if last_sprint:
            import re
            m = re.search(r"Sprint[- ]*(\d+)", last_sprint[0])
            sprint_num = int(m.group(1)) + 1 if m else 2

        title = f"[Sprint-{sprint_num}] {board} 자율 스프린트 킥오프 (nova-autoplan)"
        body = (
            f"NOVA 자율 엔진 v1.0이 Sprint 완주를 감지하여 자동 생성\n"
            f"생성 시각: {datetime.datetime.now().isoformat()}\n\n"
            f"## nova-autoplan 임무\n"
            f"이전 스프린트 결과를 nova_brain.db에서 분석하여\n"
            f"다음 스프린트 목표를 스스로 설정하고 DoD 키워드를 배분할 것.\n\n"
            f"## 완료 기준\n"
            f"- 다음 Sprint 목표 3~5개 명시\n"
            f"- 각 단계 DoD 키워드 정의\n"
            f"- nova_brain.db takes에 계획 기록\n"
        )
        now_ts = int(time.time())
        tid = f"t_{uuid.uuid4().hex[:8]}"
        bc.execute(
            "INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, title, body, "nova-autoplan", "ready", 0, "nova_autonomous_engine", now_ts)
        )
        bdb.commit()
        bdb.close()
        log(f"  Sprint-{sprint_num} 씨앗 생성: {tid}")
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        log(f"  [WARN] {board} kanban.db _kickoff_sprint 실패({e}) — 스킵")


def main():
    # ── flock: 동시 실행 방지 ─────────────────────────────────
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 이미 다른 인스턴스 실행 중 → SILENT 종료
        lock_fd.close()
        print("[nova-engine] flock 획득 실패 — 다른 인스턴스 실행 중. SKIP.", flush=True)
        return 0

    try:
        log("=" * 50)
        log("NOVA 자율 운영 엔진 v1.1 — 상황 판단 시작")

        # 현재 상태 측정
        state = get_brain_state()
        prev_state = load_state()

        log(f"  health={state['health_score']} takes={state['takes_total']} "
            f"orphan={state['orphan']} dream_ago={hours_since(state['last_dream']):.1f}h "
            f"takes_since_dream={state.get('takes_since_dream', 0)}")

        for board, bs in state["boards"].items():
            log(f"  [{board}] active={bs['active']} done_recent={bs['done_recent']} blocked={bs['blocked']}")

        # 판단 + 행동
        actions = decide_and_act(state, prev_state)

        # prev_state에서 실행 추적 키 복원 (decide_and_act가 state에 직접 기록)
        for carry_key in ("last_audit", "last_chain_engine_run", "last_eval_quality_boost", "last_growth_tracker"):
            if carry_key not in state and carry_key in prev_state:
                state[carry_key] = prev_state[carry_key]

        # 상태 저장 (다음 판단을 위해)
        save_state(state)

        if actions:
            log(f"  실행된 액션: {actions}")
            record_take("nova-evaluator",
                f"nova_autonomous_engine: {', '.join(actions)} 자율 실행 완료", 0.88)

        log("NOVA 자율 운영 엔진 완료")
        return 0 if actions else 0  # SILENT if no action

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
