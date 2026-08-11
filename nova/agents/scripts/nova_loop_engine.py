#!/usr/bin/env python3
"""
nova_loop_engine.py — NOVA 완전 자율화 루프 엔진
=================================================

아키텍처: OODA 루프 (Observe → Orient → Decide → Act → ...)

  brain_watcher (inotify, 항상 실행)
    ↓ 미시 반응 (takes 변화에 즉시 반응)
  loop_engine (이 파일, 5분 간격 or 수동)
    ↓ 거시 오케스트레이션 (구간 판정 → 하네스 체인 선택)

구간 (Phase):
  COLD     — pages < 10   → KB 인덱싱 → research 하네스 → synthesize
  WARM     — 정상 운영     → learn → dream 경량 (sync+health)
  HOT      — takes 급증    → synthesize → dream (extract+patterns) → wiki
  CRITICAL — health < 70  → dream 전체 사이클 → contradiction 해소

루프 단계:
  1. Observe  — brain.db 스냅샷 (pages, takes, health, 구간)
  2. Orient   — 구간 판정 + 쿨다운 체크
  3. Decide   — 체인 선택 (복수 엔진 순서 결정)
  4. Act      — 선택된 엔진 순차 실행 (각 결과 brain.db 기록)
  5. Record   — hermes_events에 루프 결과 기록
  6. 반복     — INTERVAL 초 후 다시 1로

데몬 모드: python3 nova_loop_engine.py --daemon
1회 모드:  python3 nova_loop_engine.py
상태 확인: python3 nova_loop_engine.py --status
"""
import os, sys, json, time, sqlite3, uuid, argparse, subprocess, fcntl, signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 환경 ──────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   Path.home() / ".nova"))
BRAIN_DB    = HERMES_HOME / "nova_brain.db"   # symlink → ~/.nova/brain.db
BIN         = HERMES_HOME / "bin"
SCRIPTS     = HERMES_HOME / "scripts"
LOG_FILE    = NOVA_HOME / "logs" / "nova_loop_engine.log"
STATE_FILE  = NOVA_HOME / "logs" / "nova_loop_state.json"
LOCK_FILE   = "/tmp/nova_loop.lock"

sys.path.insert(0, str(BIN))
sys.path.insert(0, str(Path.home() / "nova"))

# ── 루프 설정 ──────────────────────────────────────────────────────
INTERVAL    = int(os.environ.get("NOVA_LOOP_INTERVAL", 300))   # 5분
MAX_CYCLES  = int(os.environ.get("NOVA_LOOP_MAX_CYCLES", 0))   # 0 = 무한

# 구간 임계값
THRESHOLDS = {
    "cold_pages":          10,    # pages < 10 → COLD
    "hot_takes_delta":     15,    # 마지막 dream 이후 신규 takes > 15 → HOT
    # CRITICAL 임계값: score_coverage=0 상태에서 65~66점이 구조적 하한선
    # → 실질 데이터 부족 상태와 진짜 위기를 구분하기 위해 60으로 낮춤
    "critical_health":     60.0,
    # 쿨다운 (초)
    "cooldown_cold":      1800,   # COLD 루프 30분 간격
    "cooldown_warm":      3600,   # WARM 루프 1시간 간격
    "cooldown_hot":       1800,   # HOT 루프 30분 간격
    "cooldown_critical":   900,   # CRITICAL 루프 15분 간격
}

# 구간별 체인 정의
# 각 항목: (스크립트, 인자목록, timeout초, 필수여부)
CHAINS = {
    "COLD": [
        ("nova_kb_sync.py",         [],                         60,  True),
        ("nova_dream.py",           ["--phase","sync"],         30,  True),
        ("nova_learn_harvester.py", [],                        120,  False),
        ("nova_brain_synthesize.py",["--auto"],                120,  False),  # --mode 없음 → --auto
        ("nova_dream.py",           ["--phase","health"],       20,  False),
    ],
    "WARM": [
        ("nova_dream.py",           ["--phase","sync"],         30,  True),
        ("nova_learn_harvester.py", [],                        120,  False),
        ("nova_dream.py",           ["--phase","health"],       20,  False),
    ],
    "HOT": [
        ("nova_brain_synthesize.py",["--auto"],                180,  True),
        ("nova_dream.py",           ["--phase","sync"],         30,  False),
        ("nova_dream.py",           ["--phase","extract"],      60,  False),
        ("nova_dream.py",           ["--phase","patterns"],     60,  False),
        ("nova_dream.py",           ["--phase","consolidate"],  60,  False),
        ("nova_dream.py",           ["--phase","health"],       20,  False),
        ("nova_wiki_synthesize.py", ["--phase","crosslink"],    60,  False),
        # HOT: takes 급증 = 대화에서 새 지식 생산 중 → research 하네스로 심화
        ("__harness__research__",   [],                        300,  False),
    ],
    "CRITICAL": [
        ("nova_dream.py",           [],                        400,  True),   # 전체 사이클
        ("nova_dream.py",           ["--phase","contradictions"],60, False),
        ("nova_kb_sync.py",         ["--reindex-all"],         120,  False),
        ("nova_dream.py",           ["--phase","health"],       20,  True),
    ],
}


# ── 로깅 ──────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str, level: str = "INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[nova-loop] [{level}] [{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── 상태 파일 ─────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))


# ── DB 읽기 ──────────────────────────────────────────────────────
def observe() -> dict:
    """brain.db 현재 스냅샷 수집 (Observe 단계)"""
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    try:
        pages   = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
        chunks  = conn.execute("SELECT count(*) FROM page_chunks").fetchone()[0]
        takes   = conn.execute("SELECT count(*) FROM takes").fetchone()[0]
        contra  = conn.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
        orphan  = conn.execute("SELECT count(*) FROM pages WHERE agent IS NULL").fetchone()[0]
        unread  = conn.execute("SELECT count(*) FROM hermes_events WHERE is_read=0").fetchone()[0]

        bh = conn.execute(
            "SELECT score_overall, measured_at, pages_with_takes FROM brain_health "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        health      = bh["score_overall"] if bh else 60.0
        measured_at = bh["measured_at"]   if bh else ""
        pages_w_tk  = bh["pages_with_takes"] if bh else 0

        # 마지막 dream 이후 신규 takes
        dream_row = conn.execute(
            "SELECT created_at FROM agent_activity WHERE action='dream_cycle' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_dream = dream_row["created_at"] if dream_row else None

        # last_dream이 없으면(최초) takes_since_dream=0으로 처리
        # (2000-01-01 폴백 시 전체 takes가 HOT/CRITICAL 과잉 판정되는 문제 방지)
        if last_dream:
            takes_since_dream = conn.execute(
                "SELECT count(*) FROM takes WHERE created_at > ? "
                "AND holder NOT IN ('nova-evaluator','chain_engine','nova-doctor')",
                (last_dream,)
            ).fetchone()[0]
        else:
            takes_since_dream = 0   # 최초 실행 — dream 기준점 없음, HOT 판정 억제

        return {
            "pages": pages, "chunks": chunks, "takes": takes,
            "contradictions_open": contra, "orphan": orphan,
            "health": health, "measured_at": measured_at,
            "pages_with_takes": pages_w_tk,
            "unread_events": unread,
            "last_dream": last_dream,
            "takes_since_dream": takes_since_dream,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()


# ── 구간 판정 ────────────────────────────────────────────────────
def orient(snap: dict, state: dict) -> tuple[str, str]:
    """
    Observe 결과 → 구간(Phase) 판정 (Orient 단계)
    반환: (phase, reason)
    """
    T = THRESHOLDS
    health = snap["health"]
    pages  = snap["pages"]
    td     = snap["takes_since_dream"]

    if health < T["critical_health"]:
        return "CRITICAL", f"health={health:.1f} < {T['critical_health']}"
    if pages < T["cold_pages"]:
        return "COLD", f"pages={pages} < {T['cold_pages']} (KB 축적 필요)"
    if td > T["hot_takes_delta"]:
        return "HOT", f"takes_since_dream={td} > {T['hot_takes_delta']}"
    return "WARM", f"정상 운영 (health={health:.1f}, pages={pages}, td={td})"


def _secs_since(state: dict, phase: str) -> float:
    """마지막 해당 구간 실행 이후 경과 초"""
    key = f"last_{phase.lower()}_run"
    ts  = state.get(key)
    if not ts:
        return 999999.0
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 999999.0


def decide(phase: str, state: dict) -> tuple[list, str]:
    """
    구간 + 쿨다운 → 실행할 체인 결정 (Decide 단계)
    반환: (chain, skip_reason)  skip_reason이 있으면 실행 스킵
    """
    cooldown_key = f"cooldown_{phase.lower()}"
    cooldown     = THRESHOLDS.get(cooldown_key, 3600)
    elapsed      = _secs_since(state, phase)

    if elapsed < cooldown:
        remaining = int(cooldown - elapsed)
        return [], f"쿨다운 중 ({remaining}초 남음)"

    chain = CHAINS.get(phase, [])
    return chain, ""


# ── 엔진 실행 ─────────────────────────────────────────────────────
def run_engine(script: str, args: list, timeout: int, required: bool) -> dict:
    """단일 엔진 스크립트 실행, 결과 반환.
    script가 '__harness__<name>__' 형식이면 nova_run_harness() 로 처리.
    """
    # ── harness 마커 처리 ──────────────────────────────────────
    if script.startswith("__harness__") and script.endswith("__"):
        harness_name = script[len("__harness__"):-len("__")]
        topic = _get_hot_topic()
        log(f"  [harness] {harness_name} 실행 (topic={topic})")
        t0 = time.time()
        res = nova_run_harness(harness_name, context={"topic": topic}, timeout=timeout)
        dur = round(time.time() - t0, 1)
        tail = res["output"][:120]
        if res["report_path"]:
            tail += f" → {res['report_path']}"
        # harness ok=True 시 report_path 유무와 무관하게 kb_sync 실행
        if res["ok"]:
            _sync_workspace_to_kb()
        return {"script": harness_name, "ok": res["ok"], "msg": tail, "duration": dur}

    # ── 일반 스크립트 ──────────────────────────────────────────
    path = BIN / script if (BIN / script).exists() else SCRIPTS / script
    if not path.exists():
        return {"script": script, "ok": False, "msg": "파일 없음", "duration": 0}

    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(HERMES_HOME),
        "NOVA_HOME":   str(NOVA_HOME),
        "PYTHONPATH":  str(BIN) + ":" + str(Path.home() / "nova"),
    })

    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(path)] + args,
            env=env, capture_output=True, text=True, timeout=timeout,
        )
        dur  = round(time.time() - t0, 1)
        ok   = r.returncode == 0
        out  = (r.stdout + r.stderr).strip()
        tail = out.splitlines()[-1][:120] if out else "no output"
        return {"script": script, "ok": ok, "msg": tail, "duration": dur}
    except subprocess.TimeoutExpired:
        dur = round(time.time() - t0, 1)
        return {"script": script, "ok": False, "msg": f"TIMEOUT {timeout}s", "duration": dur}
    except Exception as e:
        dur = round(time.time() - t0, 1)
        return {"script": script, "ok": False, "msg": str(e)[:100], "duration": dur}


def _sync_workspace_to_kb():
    """harness 완료 후 workspace → brain.db 즉시 인덱싱."""
    try:
        r = subprocess.run(
            [sys.executable, str(BIN/"nova_kb_sync.py")],
            env={**os.environ,
                 "HERMES_HOME": str(HERMES_HOME),
                 "NOVA_HOME": str(NOVA_HOME),
                 "PYTHONPATH": str(BIN)+":"+str(Path.home()/"nova")},
            capture_output=True, text=True, timeout=60,
        )
        log(f"  kb_sync after harness: {(r.stdout+r.stderr).strip().splitlines()[-1][:80]}")
    except Exception as e:
        log(f"  kb_sync 실패: {e}", "WARN")


def act(phase: str, chain: list) -> list[dict]:
    """
    체인 순차 실행 (Act 단계)
    필수(required=True) 엔진이 실패하면 이후 체인 중단.
    """
    results = []
    for script, args, timeout, required in chain:
        arg_str = " ".join(args) if args else ""
        log(f"  ▶ {script} {arg_str} (timeout={timeout}s)")
        res = run_engine(script, args, timeout, required)
        status = "OK" if res["ok"] else "FAIL"
        log(f"  {'✓' if res['ok'] else '✗'} [{status}] {res['script']} — {res['msg']} ({res['duration']}s)")
        results.append(res)

        if not res["ok"] and required:
            log(f"  ⛔ 필수 엔진 실패 → 체인 중단", "WARN")
            break
    return results


# ── hermes_events 기록 ──────────────────────────────────────────
def push_event(event_type: str, severity: str, title: str, detail: str = ""):
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        now  = datetime.now(timezone.utc).isoformat()
        eid  = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO hermes_events "
            "(id,event_type,severity,title,detail,source_agent,created_at,is_read) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, event_type, severity, title, detail, "nova_loop_engine", now, 0)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"  [EVENT-ERR] {e}", "WARN")


# ── nova_run_harness ─────────────────────────────────────────────
def nova_run_harness(harness_name: str, context: dict = None,
                     timeout: int = 300) -> dict:
    """
    NOVA Harness를 Python API로 직접 실행.
    결과: {"ok": bool, "output": str, "report_path": str|None}
    성공 시 report.md를 kb_sync가 인덱싱할 수 있도록 workspace/에 저장됨.
    """
    try:
        import sys as _sys
        nova_src = Path.home() / "nova"
        for p in (str(BIN), str(nova_src)):
            if p not in _sys.path:
                _sys.path.insert(0, p)

        from nova.core.config import load_config
        from nova.core.harness import HarnessLoader
        from nova.core.orchestrator import Orchestrator

        cfg = load_config(str(NOVA_HOME / "nova.yaml"))
        # ~ 확장 (HarnessLoader가 직접 처리 안 함)
        cfg.harnesses_dir = str(Path(cfg.harnesses_dir).expanduser())
        cfg.workspace     = str(Path(cfg.workspace).expanduser())

        loader  = HarnessLoader(cfg.harnesses_dir)
        harness = loader.load(harness_name)
        orch    = Orchestrator(cfg)

        ctx = context or {}
        ok  = orch.run(harness, context=ctx, resume=False)

        # report.md 경로 확인
        ws   = Path(cfg.workspace) / harness_name
        report = None
        for candidate in ["report.md", "summary_report.md", "synthesis.md"]:
            p = ws / candidate
            if p.exists():
                report = str(p)
                break

        return {"ok": ok, "output": f"harness={harness_name} ok={ok}",
                "report_path": report}
    except Exception as e:
        return {"ok": False, "output": str(e)[:200], "report_path": None}


def _get_hot_topic() -> str:
    """brain.db 최근 takes에서 가장 많이 등장한 키워드 → research 주제 결정."""
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT claim FROM takes ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
        # 단어 빈도 집계
        import re
        counter: dict = {}
        for r in rows:
            for w in re.findall(r"[가-힣a-zA-Z]{3,}", r["claim"] or ""):
                if w.lower() not in {"nova","hermes","take","takes","brain","pages"}:
                    counter[w] = counter.get(w, 0) + 1
        if counter:
            return max(counter, key=counter.get)
        return "NOVA 자율화 루프"
    except Exception:
        return "NOVA 자율화 루프"


def push_take(claim: str, holder: str = "nova_loop_engine", kind: str = "insight"):
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        now = datetime.now(timezone.utc).isoformat()
        tid = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT OR IGNORE INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, None, kind, holder, claim[:200], 0.8, now, now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"  [TAKE-ERR] {e}", "WARN")


# ── link_orphan_takes ─────────────────────────────────────────────
def link_orphan_takes(dry_run: bool = False) -> int:
    """
    page_id=NULL인 take들을 BM25로 가장 유사한 page에 연결.
    pages_with_takes 상승 → score_coverage 상승 → health 상승 경로 복구.
    반환: 연결된 take 수
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row

        orphans = conn.execute(
            "SELECT id, claim FROM takes WHERE page_id IS NULL OR page_id = '' "
            "LIMIT 100"  # 한 번에 100개씩
        ).fetchall()

        if not orphans:
            conn.close()
            return 0

        pages = conn.execute(
            "SELECT id, path, title, compiled_truth FROM pages LIMIT 200"
        ).fetchall()

        if not pages:
            conn.close()
            return 0

        import re

        def tokenize(text: str) -> list:
            return re.findall(r"[a-zA-Z가-힣][a-zA-Z가-힣0-9_-]{1,}", (text or "").lower())

        def bm25(query_toks: list, doc_toks: list) -> float:
            tf = {}
            for t in doc_toks:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            dl = len(doc_toks)
            for t in set(query_toks):
                f = tf.get(t, 0)
                score += f / (f + 1.5 * (1 - 0.75 + 0.75 * dl / 300))
            return score

        linked = 0
        for take in orphans:
            claim_toks = tokenize(take["claim"])
            if len(claim_toks) < 2:
                continue
            best_id, best_score = None, 0.0
            for page in pages:
                doc = (page["title"] or "") + " " + (page["compiled_truth"] or "")[:500]
                score = bm25(claim_toks, tokenize(doc))
                if score > best_score:
                    best_score, best_id = score, page["id"]

            if best_id and best_score > 1.5:  # 임계값 상향: 0.5→1.5 (위양성 방지)
                if not dry_run:
                    conn.execute(
                        "UPDATE takes SET page_id=? WHERE id=?",
                        (best_id, take["id"])
                    )
                linked += 1

        if not dry_run and linked:
            conn.commit()
            log(f"  link_orphan_takes: {linked}개 연결 완료 (best_score≥1.5)")
        conn.close()
        return linked
    except Exception as e:
        log(f"  [LINK-ERR] {e}", "WARN")
        return 0


# ── 단일 루프 사이클 ──────────────────────────────────────────────
def run_cycle(cycle: int, state: dict) -> dict:
    """한 번의 OODA 사이클 실행, 갱신된 state 반환"""
    log(f"━━━ 사이클 #{cycle} 시작 ━━━")

    # 1. Observe
    try:
        snap = observe()
    except Exception as e:
        log(f"  [OBSERVE-ERR] {e}", "ERROR")
        return state

    log(f"  Observe: pages={snap['pages']} chunks={snap['chunks']} "
        f"takes={snap['takes']} health={snap['health']:.1f} "
        f"td={snap['takes_since_dream']} contra={snap['contradictions_open']}")

    # 2. Orient
    phase, reason = orient(snap, state)
    log(f"  Orient:  phase={phase} — {reason}")

    # 3. Decide
    chain, skip_reason = decide(phase, state)
    if skip_reason:
        log(f"  Decide:  SKIP — {skip_reason}")
        state["last_snapshot"] = snap
        return state

    log(f"  Decide:  {phase} 체인 {len(chain)}단계 실행")

    # 4. Act
    t0 = time.time()
    # 모든 구간 실행 전: orphan takes 연결 (health 상승 경로 복구)
    n_linked = link_orphan_takes()
    if n_linked:
        log(f"  Pre-Act: orphan takes {n_linked}개 page 연결 완료")
    results = act(phase, chain)
    elapsed = round(time.time() - t0, 1)

    ok_cnt   = sum(1 for r in results if r["ok"])
    fail_cnt = len(results) - ok_cnt

    log(f"  Act:     완료 {ok_cnt}/{len(results)} ({elapsed}s) — "
        f"FAIL {fail_cnt}개")

    # 5. Record
    now_iso  = datetime.now(timezone.utc).isoformat()
    state[f"last_{phase.lower()}_run"] = now_iso
    state["last_cycle"]  = cycle
    state["last_run_at"] = now_iso
    state["last_phase"]  = phase
    state["last_snapshot"] = snap

    # take + event 기록
    summary = f"루프사이클#{cycle} [{phase}] {ok_cnt}/{len(results)}성공 {elapsed}s"
    push_take(summary)

    if fail_cnt > 0:
        failed = [r["script"] for r in results if not r["ok"]]
        push_event("loop_fail", "WARN",
                   f"루프 [{phase}] 일부 실패",
                   f"실패: {failed} | {summary}")
    elif phase == "CRITICAL":
        push_event("loop_critical", "INFO",
                   f"CRITICAL 루프 완료",
                   f"health={snap['health']:.1f} → {summary}")

    log(f"━━━ 사이클 #{cycle} 완료 — phase={phase} {ok_cnt}/{len(results)} ({elapsed}s) ━━━")
    return state


# ── 상태 출력 ─────────────────────────────────────────────────────
def print_status():
    state = load_state()
    try:
        snap  = observe()
    except Exception as e:
        print(f"[ERROR] observe 실패: {e}")
        return

    phase, reason = orient(snap, state)

    print()
    print("╔══════════════════════════════════════════════╗")
    print("║     NOVA Loop Engine 상태                   ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  현재 구간:    {phase}  ({reason})")
    print(f"  brain.db:")
    print(f"    pages       = {snap['pages']}  (chunks={snap['chunks']})")
    print(f"    takes       = {snap['takes']}  (since_dream={snap['takes_since_dream']})")
    print(f"    health      = {snap['health']:.1f}/100")
    print(f"    contra_open = {snap['contradictions_open']}")
    print(f"    unread_evt  = {snap['unread_events']}")
    print()
    for ph in ("COLD","WARM","HOT","CRITICAL"):
        cd_key = f"cooldown_{ph.lower()}"
        cd     = THRESHOLDS.get(cd_key, 3600)
        el     = _secs_since(state, ph)
        last   = state.get(f"last_{ph.lower()}_run", "없음")
        if isinstance(last, str) and len(last) > 19:
            last = last[:19]
        remain = max(0, cd - el)
        status = "준비" if remain == 0 else f"쿨다운 {int(remain)}초"
        print(f"  [{ph:8s}] 마지막={last}  {status}")
    print()

    # 로그 최근 5줄
    if LOG_FILE.exists():
        tail = LOG_FILE.read_text().strip().splitlines()[-5:]
        print("  최근 로그:")
        for l in tail:
            print(f"    {l}")
    print()


# ── 메인 ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NOVA Loop Engine")
    parser.add_argument("--daemon",  action="store_true", help="데몬 모드 (무한 루프)")
    parser.add_argument("--status",  action="store_true", help="현재 상태 출력 후 종료")
    parser.add_argument("--phase",   choices=["COLD","WARM","HOT","CRITICAL"],
                        help="구간 강제 지정 (1회 실행)")
    parser.add_argument("--cycles",  type=int, default=0, help="최대 사이클 수 (0=무한)")
    parser.add_argument("--interval",type=int, default=INTERVAL, help=f"루프 간격 초 (기본={INTERVAL})")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    # flock — 중복 실행 방지
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[nova-loop] 이미 실행 중 (flock)")
        sys.exit(77)

    max_cycles = args.cycles or MAX_CYCLES
    interval   = args.interval

    if args.daemon:
        log(f"데몬 모드 시작 — interval={interval}s max_cycles={'∞' if not max_cycles else max_cycles}")
    elif args.phase:
        log(f"1회 실행 — 강제 구간={args.phase}")
    else:
        log("1회 실행")

    state  = load_state()
    cycle  = state.get("last_cycle", 0)

    def _shutdown(sig, frame):
        log("종료 신호 수신 — 루프 종료")
        save_state(state)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    try:
        while True:
            cycle += 1

            if args.phase:
                # 강제 구간 — 쿨다운 무시
                log(f"━━━ 사이클 #{cycle} [강제 {args.phase}] ━━━")
                snap    = observe()
                chain   = CHAINS[args.phase]
                results = act(args.phase, chain)
                ok_cnt  = sum(1 for r in results if r["ok"])
                log(f"━━━ 완료 {ok_cnt}/{len(results)} ━━━")
                save_state(state)
                break

            state = run_cycle(cycle, state)
            save_state(state)

            if not args.daemon:
                break

            if max_cycles and cycle >= max_cycles:
                log(f"최대 사이클 {max_cycles}회 도달 — 종료")
                break

            log(f"  다음 사이클까지 {interval}초 대기...")
            time.sleep(interval)

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
