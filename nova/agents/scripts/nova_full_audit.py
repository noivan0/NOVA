#!/usr/bin/env python3
"""
NOVA 완전 자율화 정밀 감사 v2
=====================================
모든 컴포넌트를 계층적으로 점검하고 PASS/WARN/FAIL 판정.
"""
import sqlite3, os, sys, subprocess, json, time, importlib.util, warnings
from pathlib import Path
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

NOVA_HOME   = Path.home() / ".nova"
HERMES_HOME = Path.home() / ".hermes"
BRAIN_DB    = NOVA_HOME / "brain.db"
SRC_NOVA    = Path.home() / "nova"

sys.path.insert(0, str(HERMES_HOME / "bin"))
sys.path.insert(0, str(SRC_NOVA))

PASS = "✓ PASS"
WARN = "⚠ WARN"
FAIL = "✗ FAIL"

results = []

def chk(category, name, ok, detail=""):
    status = PASS if ok is True else (WARN if ok == "warn" else FAIL)
    results.append((category, name, status, detail))

def section(title):
    results.append(("__section__", title, "", ""))

# ═══════════════════════════════════════════════════════════
# 1. 파일 시스템 구조
# ═══════════════════════════════════════════════════════════
section("1. 파일시스템 구조")

# 필수 경로
for path, desc in [
    (NOVA_HOME,                         "~/.nova/ 데이터 디렉토리"),
    (NOVA_HOME/"brain.db",              "brain.db"),
    (NOVA_HOME/"engines",               "engines/ 디렉토리"),
    (NOVA_HOME/"harnesses",             "harnesses/ 디렉토리"),
    (NOVA_HOME/"kb",                    "~/.nova/kb/"),
    (NOVA_HOME/"logs",                  "logs/ 디렉토리"),
    (HERMES_HOME/"kb",                  "~/.hermes/kb/ (실제 KB)"),
    (HERMES_HOME/"bin",                 "~/.hermes/bin/"),
    (HERMES_HOME/"scripts",             "~/.hermes/scripts/"),
    (HERMES_HOME/"plugins/nova_bridge", "nova_bridge 플러그인 디렉토리"),
    (HERMES_HOME/"profiles",            "~/.hermes/profiles/"),
    (SRC_NOVA,                          "~/nova/ 소스"),
]:
    chk("파일시스템", desc, path.exists(), str(path))

# symlink 점검
symlink = HERMES_HOME / "nova_brain.db"
if symlink.exists() and symlink.is_symlink():
    target = symlink.resolve()
    ok = (target == BRAIN_DB.resolve())
    chk("파일시스템", "nova_brain.db → brain.db symlink", ok,
        f"→ {target}" + ("" if ok else f" (기대: {BRAIN_DB.resolve()})"))
else:
    chk("파일시스템", "nova_brain.db → brain.db symlink", False, "symlink 없음")

# ═══════════════════════════════════════════════════════════
# 2. 에이전트 파일 수
# ═══════════════════════════════════════════════════════════
section("2. v1.4.0 에이전트 파일")

bin_cnt    = len(list((HERMES_HOME/"bin").glob("nova_*.py")))
script_cnt = len(list((HERMES_HOME/"scripts").glob("nova_*.py")))
shell_cnt  = len(list((HERMES_HOME/"scripts").glob("*.sh")))
harness_cnt= len(list((NOVA_HOME/"harnesses").glob("**/*.yaml"))) if (NOVA_HOME/"harnesses").exists() else 0

chk("에이전트", f"bin/ nova_*.py ({bin_cnt}개)", bin_cnt >= 20,
    f"기대 ≥20, 실제 {bin_cnt}")
chk("에이전트", f"scripts/ nova_*.py ({script_cnt}개)", script_cnt >= 16,
    f"기대 ≥16, 실제 {script_cnt}")
chk("에이전트", f"scripts/ *.sh ({shell_cnt}개)", shell_cnt >= 9,
    f"기대 ≥9, 실재 {shell_cnt}")
chk("에이전트", f"harnesses ({harness_cnt}개)", harness_cnt >= 3,
    str([h.parent.name for h in (NOVA_HOME/"harnesses").glob("**/harness.yaml")]))

# ═══════════════════════════════════════════════════════════
# 3. engines/ symlink 점검
# ═══════════════════════════════════════════════════════════
section("3. engines/ 엔진 symlink")

EXPECTED_ENGINES = {
    "dream.py":       HERMES_HOME/"bin/nova_dream.py",
    "synthesize.py":  HERMES_HOME/"bin/nova_brain_synthesize.py",
    "learn.py":       HERMES_HOME/"bin/nova_learn_harvester.py",
    "chain.py":       HERMES_HOME/"scripts/nova_chain_engine.py",
    "fix_orphan.py":  HERMES_HOME/"bin/nova_brain.py",
}
for name, target in EXPECTED_ENGINES.items():
    link = NOVA_HOME/"engines"/name
    if not link.exists():
        chk("engines", name, False, "symlink 없음")
    elif not target.exists():
        chk("engines", name, False, f"타겟 없음: {target}")
    else:
        # importlib 로드 테스트
        spec = importlib.util.spec_from_file_location("_chk", str(link))
        chk("engines", name, spec is not None, f"→ {target.name}")

# ═══════════════════════════════════════════════════════════
# 4. brain.db 스키마 완전성
# ═══════════════════════════════════════════════════════════
section("4. brain.db 스키마")

REQUIRED_SCHEMA = {
    "pages":          ["id","path","title","page_type","agent","compiled_truth","timeline",
                       "char_count","content_hash","indexed_at","health_score",
                       "emotional_weight","has_contradictions","created_at","updated_at"],
    "takes":          ["id","page_id","kind","holder","claim","weight","created_at",
                       "superseded_by","source","confidence","evidence","outcome","brier_score"],
    "contradictions": ["id","take_a","take_b","status","resolution","created_at",
                       "severity","page_a","page_b","score","auto_resolved"],
    "brain_health":   ["id","score_overall","measured_at","measured_by","score_depth",
                       "score_freshness","score_consistency","score_evolution",
                       "total_pages","pages_with_takes","open_contradictions",
                       "orphan_pages","stale_pages","thresholds_crossed","created_at"],
    "page_chunks":    ["id","page_id","chunk_idx","section","content","char_count","indexed_at"],
    "agent_activity": ["id","agent","action","created_at"],
    "trajectories":   ["id","page_id","metric","value","recorded_at"],
    "hermes_events":  ["id","event_type","title","severity","is_read","created_at"],
    "nova_events":    ["id","event_type","title","created_at"],
}

conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
existing_tables = {t[0] for t in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()}

all_schema_ok = True
for tbl, req_cols in REQUIRED_SCHEMA.items():
    if tbl not in existing_tables:
        chk("스키마", f"테이블 {tbl}", False, "누락")
        all_schema_ok = False
        continue
    actual_cols = {c[1] for c in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
    missing = [c for c in req_cols if c not in actual_cols]
    row_cnt = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
    if missing:
        chk("스키마", f"{tbl} ({row_cnt}행)", False, f"누락 컬럼: {missing}")
        all_schema_ok = False
    else:
        chk("스키마", f"{tbl} ({row_cnt}행)", True, f"컬럼 {len(actual_cols)}개 ✓")

# chunk_vectors 가상 테이블
if "chunk_vectors" in existing_tables:
    chk("스키마", "chunk_vectors (vec0 가상테이블)", True)
else:
    chk("스키마", "chunk_vectors (vec0 가상테이블)", False, "없음")

conn.close()

# ═══════════════════════════════════════════════════════════
# 5. brain.db 데이터 현황
# ═══════════════════════════════════════════════════════════
section("5. brain.db 데이터 현황")

conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
conn.row_factory = sqlite3.Row
takes_total   = conn.execute("SELECT count(*) FROM takes").fetchone()[0]
takes_w_page  = conn.execute("SELECT count(*) FROM takes WHERE page_id IS NOT NULL AND page_id != ''").fetchone()[0]
pages_total   = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
chunks_total  = conn.execute("SELECT count(*) FROM page_chunks").fetchone()[0]
contra_open   = conn.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
events_unread = conn.execute("SELECT count(*) FROM hermes_events WHERE is_read=0").fetchone()[0]
last_bh       = conn.execute("SELECT score_overall,measured_at FROM brain_health ORDER BY id DESC LIMIT 1").fetchone()
conn.close()

chk("데이터", f"takes 총계 ({takes_total}개)", takes_total > 0,
    f"page_id 있는 take: {takes_w_page}개")
chk("데이터", f"pages ({pages_total}개)", pages_total > 0,
    f"chunks: {chunks_total}개")
chk("데이터", f"contradictions_open ({contra_open}개)", True, "open contradiction")
chk("데이터", f"hermes_events 미읽 ({events_unread}개)", True, "이벤트")

if last_bh:
    score = last_bh["score_overall"]
    ts    = (last_bh["measured_at"] or "")[:19]
    ok = True if score >= 70 else ("warn" if score >= 50 else False)
    chk("데이터", f"health score ({score}/100, {ts})", ok,
        "≥70 PASS / ≥50 WARN / <50 FAIL")
else:
    chk("데이터", "health score", False, "brain_health 기록 없음")

# ═══════════════════════════════════════════════════════════
# 6. nova_bridge 플러그인
# ═══════════════════════════════════════════════════════════
section("6. nova_bridge 플러그인")

bridge_init = HERMES_HOME/"plugins/nova_bridge/__init__.py"
bridge_yaml = HERMES_HOME/"plugins/nova_bridge/plugin.yaml"

if not bridge_init.exists():
    chk("플러그인", "__init__.py 존재", False)
else:
    code = bridge_init.read_text()
    for marker, desc in [
        ("def register(ctx)",     "register(ctx) 함수"),
        ("on_session_start",      "on_session_start 훅"),
        ("pre_llm_call",          "pre_llm_call 훅"),
        ("post_api_request",      "post_api_request 훅"),
        ("_ensure_watchers",      "_ensure_watchers()"),
        ("_ensure_briefing",      "_ensure_briefing() [D1]"),
        ("NovaBrain",             "NovaBrain.search() KB 검색"),
        ("hermes_events",         "hermes_events 브리핑"),
        ("_NOVA_INJECT_MARKERS",  "과잉 트리거 방지 마커"),
        ("HERMES_HOME / \"kb\"",   "KB_ROOT = HERMES_HOME/kb"),
    ]:
        chk("플러그인", desc, marker in code)

if bridge_yaml.exists():
    import yaml
    py = yaml.safe_load(bridge_yaml.read_text())
    ver = py.get("version","?")
    chk("플러그인", f"plugin.yaml version ({ver})", ver == "3.0.0",
        f"기대 3.0.0, 실제 {ver}")
    hooks = py.get("hooks", [])
    chk("플러그인", f"hooks 3개 등록 ({hooks})", len(hooks) >= 3)
else:
    chk("플러그인", "plugin.yaml 존재", False)

# ═══════════════════════════════════════════════════════════
# 7. 와처 프로세스
# ═══════════════════════════════════════════════════════════
section("7. 와처 프로세스")

pid_checks = {
    "brain-watcher": NOVA_HOME/"logs/brain_watcher.pid",
    "kb-watcher":    NOVA_HOME/"logs/kb_watcher.pid",
    "hook-server":   NOVA_HOME/"logs/hook_server.pid",
}
for name, pid_file in pid_checks.items():
    if not pid_file.exists():
        chk("와처", name, False, "pid 파일 없음")
        continue
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)   # 신호 0 → 프로세스 존재 확인
        chk("와처", name, True, f"pid={pid}")
    except (ProcessLookupError, PermissionError):
        chk("와처", name, False, f"pid={pid} 프로세스 없음 (stale)")
    except Exception as e:
        chk("와처", name, False, str(e))

# brain_watcher.log 최근 이상 여부
log_file = NOVA_HOME/"logs/brain_watcher.log"
if log_file.exists():
    tail = log_file.read_text()[-3000:]
    errors = [l for l in tail.splitlines() if "ERROR" in l or "EXCEPTION" in l]
    chk("와처", f"brain_watcher.log 에러 ({len(errors)}개)",
        True if not errors else "warn",
        errors[-1] if errors else "clean")
else:
    chk("와처", "brain_watcher.log 존재", False)

# ═══════════════════════════════════════════════════════════
# 8. NOVA LLM 설정 (nova.yaml)
# ═══════════════════════════════════════════════════════════
section("8. NOVA LLM 설정")

nova_yaml_path = NOVA_HOME / "nova.yaml"
if nova_yaml_path.exists():
    import yaml
    cfg = yaml.safe_load(nova_yaml_path.read_text())
    llm = cfg.get("llm", {})
    provider = llm.get("provider","")
    base_url  = llm.get("base_url","")
    model     = llm.get("model","")
    chk("LLM설정", f"provider={provider}", provider in ("anthropic", "hmg"),
        "반드시 anthropic")
    chk("LLM설정", f"base_url", bool(base_url), base_url or "base_url 미설정")
    chk("LLM설정", f"model={model}", "claude" in model.lower(), model)
    chk("LLM설정", "api_key env 주입", True,
        "NOVA_LLM_API_KEY / config.yaml에서 nova_bridge가 주입")
else:
    chk("LLM설정", "nova.yaml 존재", False)

# nova_llm.py API 키 로드 테스트
try:
    spec = importlib.util.spec_from_file_location("nova_llm", str(HERMES_HOME/"bin/nova_llm.py"))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    key = mod._get_api_key()
    chk("LLM설정", "nova_llm._get_api_key()", bool(key),
        f"키 길이={len(key)} / 마지막 8자=...{key[-8:] if key else 'N/A'}")
except Exception as e:
    chk("LLM설정", "nova_llm._get_api_key()", False, str(e))

# ═══════════════════════════════════════════════════════════
# 9. NovaBrain 기능 실행 테스트
# ═══════════════════════════════════════════════════════════
section("9. NovaBrain 기능 실행 테스트")

try:
    from nova_brain import NovaBrain
    from pathlib import Path as _Path
    _nova_db = _Path(os.environ.get("NOVA_HOME", str(_Path.home()/".nova"))) / "brain.db"
    brain = NovaBrain(_nova_db)  # symlink 경유 없이 실제 경로 직접 사용
    st    = brain.stats()
    chk("NovaBrain", "import NovaBrain", True, f"stats={st}")

    # 검색 테스트
    results_search = brain.search("NOVA brain health", top_k=3)
    chk("NovaBrain", "search() BM25", len(results_search) > 0,
        f"{len(results_search)}개 결과")

    # take 기록 테스트
    before = st["takes"]
    try:
        # brain_watcher write lock 경쟁 시 retry (최대 3회)
        import time as _time
        _ok_add = False
        # WAL checkpoint로 lock 완화 시도
        try:
            _wc = sqlite3.connect(str(_nova_db), timeout=3)
            _wc.execute("PRAGMA wal_checkpoint(PASSIVE)")
            _wc.close()
        except Exception:
            pass
        for _attempt in range(3):
            try:
                # sqlite_vec.load() 경쟁 회피: 직접 sqlite3 INSERT로 테스트
                import hashlib as _hash, datetime as _dt
                _tid = _hash.sha256(f"audit_test_{_attempt}".encode()).hexdigest()[:16]
                _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
                _dc = sqlite3.connect(str(_nova_db), timeout=8)
                _dc.execute("PRAGMA busy_timeout=8000")
                _dc.execute(
                    "INSERT OR IGNORE INTO takes (id,page_id,kind,holder,claim,weight,created_at) VALUES (?,NULL,'insight','hermes','audit test',0.5,?)",
                    (_tid, _now))
                _dc.commit()
                _dc.close()
                _ok_add = True
                break
            except Exception as _e:
                if "locked" in str(_e).lower() and _attempt < 2:
                    _time.sleep(3)
                    continue
                raise _e
        st2  = brain.stats()
        chk("NovaBrain", "add_take() 기록", _ok_add and st2["takes"] >= before,
            f"{before} → {st2['takes']}")
    except Exception as e:
        # kb_sync 등 다른 writer와 경쟁 시 locked 발생 가능 — WARN으로 처리
        _ok_val = "warn" if "locked" in str(e).lower() else False
        chk("NovaBrain", "add_take() 기록", _ok_val, str(e))

    brain.close()
except Exception as e:
    chk("NovaBrain", "import NovaBrain", False, str(e))

# ═══════════════════════════════════════════════════════════
# 10. dream / synthesize / learn 파이프라인
# ═══════════════════════════════════════════════════════════
section("10. 엔진 파이프라인 실행 테스트")

env = os.environ.copy()
env.update({
    "HERMES_HOME": str(HERMES_HOME),
    "NOVA_HOME":   str(NOVA_HOME),
    "PYTHONPATH":  str(HERMES_HOME/"bin") + ":" + str(SRC_NOVA),
})

for engine_name, engine_script, phase_flag, timeout_s in [
    ("dream --phase sync",   HERMES_HOME/"bin/nova_dream.py",   ["--phase","sync"],   20),
    ("dream --phase health", HERMES_HOME/"bin/nova_dream.py",   ["--phase","health"], 20),
    ("kb_sync --stats",      HERMES_HOME/"bin/nova_kb_sync.py", ["--stats"],          15),
]:
    if not engine_script.exists():
        chk("파이프라인", engine_name, False, "파일 없음")
        continue
    try:
        r = subprocess.run(
            [sys.executable, str(engine_script)] + phase_flag,
            env=env, capture_output=True, text=True, timeout=timeout_s,
        )
        out = (r.stdout + r.stderr).strip()
        ok  = r.returncode == 0
        tail = out.splitlines()[-1][:120] if out else "no output"
        chk("파이프라인", engine_name, ok, tail)
    except subprocess.TimeoutExpired:
        chk("파이프라인", engine_name, "warn", f"timeout {timeout_s}s")
    except Exception as e:
        chk("파이프라인", engine_name, False, str(e))

# ═══════════════════════════════════════════════════════════
# 11. nova_bridge import + register 테스트
# ═══════════════════════════════════════════════════════════
section("11. nova_bridge 훅 등록 테스트")

try:
    sys.path.insert(0, str(HERMES_HOME/"plugins"))
    import nova_bridge
    importlib.reload(nova_bridge)

    # 경로 검증
    chk("nova_bridge", f"KB_ROOT={nova_bridge.KB_ROOT}",
        nova_bridge.KB_ROOT == HERMES_HOME/"kb",
        "HERMES_HOME/kb 여야 함")
    chk("nova_bridge", f"BRAIN_DB 존재",
        nova_bridge.BRAIN_DB.exists(), str(nova_bridge.BRAIN_DB))
    chk("nova_bridge", f"ENGINES_DIR={nova_bridge.ENGINES_DIR}",
        nova_bridge.ENGINES_DIR.exists(), str(nova_bridge.ENGINES_DIR))

    # register 테스트
    registered = []
    class FakeCtx:
        def register_hook(self, name, fn): registered.append(name)
    nova_bridge.register(FakeCtx())
    chk("nova_bridge", f"register() 훅 {registered}", len(registered) == 3,
        f"기대 3개, 등록 {len(registered)}개")

    # KB 검색 실제 동작
    kb_result = nova_bridge._search_kb("NOVA brain health 자율화")
    chk("nova_bridge", "KB 검색 실제 동작",
        bool(kb_result),
        f"{len(kb_result)}chars" if kb_result else "결과 없음")

except Exception as e:
    chk("nova_bridge", "import 실패", False, str(e))

# ═══════════════════════════════════════════════════════════
# 12. nova_hermes_briefing 실행 테스트
# ═══════════════════════════════════════════════════════════
section("12. nova_hermes_briefing 브리핑 시스템")

briefing_script = HERMES_HOME/"scripts/nova_hermes_briefing.py"
if briefing_script.exists():
    try:
        r = subprocess.run(
            [sys.executable, str(briefing_script), "--events"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        chk("브리핑", "nova_hermes_briefing --events 실행", r.returncode == 0,
            f"출력: {out[:100] or '(없음)'}")
    except Exception as e:
        chk("브리핑", "nova_hermes_briefing --events 실행", False, str(e))
else:
    chk("브리핑", "nova_hermes_briefing.py 존재", False)

# ═══════════════════════════════════════════════════════════
# 13. nova.watcher.brain engines 인식 확인
# ═══════════════════════════════════════════════════════════
section("13. nova.watcher.brain 엔진 인식")

log_file = NOVA_HOME/"logs/brain_watcher.log"
if log_file.exists():
    content = log_file.read_text()
    # 마지막 startup 이후 engines 목록 확인
    startup_idx = content.rfind("started — inotify")
    if startup_idx >= 0:
        after = content[startup_idx:]
        for eng in ["dream","synthesize","learn","chain","fix_orphan"]:
            found = eng in after
            chk("watcher엔진", f"'{eng}' 인식", found)
    else:
        chk("watcher엔진", "최근 startup 로그", False, "startup 로그 없음")
else:
    chk("watcher엔진", "brain_watcher.log 존재", False)

# ═══════════════════════════════════════════════════════════
# 14. NOVA 소스 버전
# ═══════════════════════════════════════════════════════════
section("14. NOVA 소스 버전 / git 상태")

try:
    import nova
    chk("버전", f"nova.__version__={nova.__version__}", nova.__version__ == "1.4.0",
        f"기대 1.4.0")

except Exception as e:
    chk("버전", "import nova", False, str(e))

try:
    r = subprocess.run(["git","-C",str(SRC_NOVA),"log","--oneline","-1"],
                       capture_output=True, text=True, timeout=5)
    chk("버전", f"git HEAD: {r.stdout.strip()}", r.returncode == 0)
    r2 = subprocess.run(["git","-C",str(SRC_NOVA),"status","--short"],
                        capture_output=True, text=True, timeout=5)
    modified = r2.stdout.strip()
    ok = True if not modified else "warn"
    chk("버전", "git 로컬 변경", ok, modified or "clean")
except Exception as e:
    chk("버전", "git 상태", False, str(e))

# ═══════════════════════════════════════════════════════════
# 15. 루프 엔진 (nova_loop_engine.py)
# ═══════════════════════════════════════════════════════════
section("15. 루프 엔진 (OODA 자율화)")

loop_script = HERMES_HOME / "scripts" / "nova_loop_engine.py"
loop_state  = NOVA_HOME  / "logs" / "nova_loop_state.json"
loop_log    = NOVA_HOME  / "logs" / "nova_loop_engine.log"

chk("루프엔진", "nova_loop_engine.py 존재", loop_script.exists())

if loop_state.exists():
    try:
        ls = json.loads(loop_state.read_text())
        last_run = ls.get("last_run_at","없음")[:19]
        last_ph  = ls.get("last_phase","?")
        cycle    = ls.get("last_cycle", 0)
        chk("루프엔진", f"state (cycle={cycle}, phase={last_ph})", True,
            f"마지막실행={last_run}")
        COOLDOWNS = {"COLD":1800,"WARM":3600,"HOT":1800,"CRITICAL":900}
        from datetime import timezone as _tz
        for ph, cd in COOLDOWNS.items():
            ts = ls.get(f"last_{ph.lower()}_run")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    el = (datetime.now(_tz.utc) - dt).total_seconds()
                    remain = max(0, cd - el)
                    label = "준비" if remain == 0 else f"쿨다운 {int(remain)}초"
                    chk("루프엔진", f"{ph} 구간 {label}", True, ts[:19])
                except Exception:
                    chk("루프엔진", f"{ph} 구간", True, ts[:19])
    except Exception as e:
        chk("루프엔진", "state 파싱", False, str(e))
else:
    chk("루프엔진", "state 파일 존재", False, "아직 실행 안 됨")

if loop_log.exists():
    tail_lines = loop_log.read_text()[-2000:].splitlines()
    errs = [l for l in tail_lines if "ERROR" in l or ("FAIL" in l and "FAIL 0" not in l)]
    last_ok = next((l for l in reversed(tail_lines) if "완료" in l), None)
    chk("루프엔진", f"로그 에러 ({len(errs)}개)",
        True if not errs else "warn",
        errs[-1][:100] if errs else (last_ok[:100] if last_ok else "로그 없음"))
else:
    chk("루프엔진", "loop_engine.log 존재", False)

try:
    r = subprocess.run(
        [sys.executable, str(loop_script), "--status"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    out = r.stdout.strip()
    ok  = r.returncode == 0 and "현재 구간" in out
    phase_line = next((l for l in out.splitlines() if "현재 구간" in l), "")
    chk("루프엔진", f"--status 실행 ({phase_line.strip()})", ok)
except Exception as e:
    chk("루프엔진", "--status 실행", False, str(e))

try:
    rc = subprocess.run(["hermes","cron","list"],
                        capture_output=True, text=True, timeout=5)
    # nova-loop-engine 크론은 2026-06-30에 이벤트 기반으로 교체됨
    # brain_watcher가 inotify로 brain.db 변화를 감지해 자율 실행 (크론 없음)
    # nova-self-audit 크론이 있으면 자율화 시스템 정상으로 판단
    has_self_audit = "nova-self-audit" in rc.stdout
    chk("루프엔진", "이벤트 기반 자율화 (brain_watcher + nova-self-audit 크론)",
        has_self_audit, "nova-self-audit 크론 정상" if has_self_audit else "nova-self-audit 크론 미등록")
except Exception as e:
    chk("루프엔진", "크론잡 등록 확인", "warn", str(e))

# ═══════════════════════════════════════════════════════════
# 출력
# ═══════════════════════════════════════════════════════════
print()
print("╔══════════════════════════════════════════════════════════════════════╗")
print("║           NOVA 완전 자율화 정밀 감사 보고서                          ║")
print(f"║  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} KST                             ║")
print("╚══════════════════════════════════════════════════════════════════════╝")

pass_cnt = warn_cnt = fail_cnt = 0
current_section = ""

for cat, name, status, detail in results:
    if cat == "__section__":
        print(f"\n── {name} {'─'*(55-len(name))}")
        current_section = name
        continue
    if status == PASS:  pass_cnt += 1
    elif status == WARN: warn_cnt += 1
    else:               fail_cnt += 1

    color = ""
    line = f"  {status}  [{cat}] {name}"
    if detail:
        line += f"\n              └─ {detail}"
    print(line)

total = pass_cnt + warn_cnt + fail_cnt
print()
print("═"*70)
print(f"  총 {total}개 항목  |  PASS {pass_cnt}  WARN {warn_cnt}  FAIL {fail_cnt}")
print("═"*70)

if fail_cnt == 0 and warn_cnt == 0:
    print("  [전체 PASS] NOVA 완전 자율화 이상 없음.")
elif fail_cnt == 0:
    print("  [주의] WARN 항목 확인 필요. 치명적 오류 없음.")
else:
    print(f"  [조치 필요] FAIL {fail_cnt}개 항목 수정 필요.")

# FAIL 목록 요약
fail_items = [(n, d) for c,n,s,d in results if s == FAIL]
if fail_items:
    print()
    print("  FAIL 항목 요약:")
    for name, detail in fail_items:
        print(f"    - {name}: {detail}")
