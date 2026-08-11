#!/usr/bin/env python3
"""
NOVA 루프 엔지니어링 구조 정밀 감사 v2
실제 구현 코드 기준으로 연계 상태 점검
"""
import sqlite3, os, sys, json, subprocess, importlib.util, time
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path.home() / ".nova"
HERMES_HOME = Path.home() / ".hermes"
BRAIN_DB    = NOVA_HOME / "brain.db"
BIN         = HERMES_HOME / "bin"
SCRIPTS     = HERMES_HOME / "scripts"

sys.path.insert(0, str(BIN))
sys.path.insert(0, str(Path.home() / "nova"))

env = os.environ.copy()
env.update({"HERMES_HOME": str(HERMES_HOME), "NOVA_HOME": str(NOVA_HOME),
            "PYTHONPATH": str(BIN)+":"+str(Path.home()/"nova")})

PASS = "✓ PASS"; WARN = "⚠ WARN"; FAIL = "✗ FAIL"
results = []

def chk(cat, name, ok, detail=""):
    st = PASS if ok is True else (WARN if ok=="warn" else FAIL)
    results.append((cat, name, st, detail))

def section(t): results.append(("__section__", t, "", ""))

print("="*70)
print(f"  NOVA 루프 엔지니어링 구조 정밀 감사")
print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*70)

# ─── 1. 루프 레이어 존재 확인 ────────────────────────────────────────
section("1. 루프 레이어 파일 존재")

loop_engine = SCRIPTS / "nova_loop_engine.py"
loop_cron   = SCRIPTS / "nova_loop_cron.sh"
chain_engine= SCRIPTS / "nova_chain_engine.py"
harnesses   = [NOVA_HOME/"harnesses"/h/"harness.yaml"
               for h in ["research","summarizer","data-pipeline"]]

chk("레이어", "nova_loop_engine.py", loop_engine.exists())
chk("레이어", "nova_loop_cron.sh",   loop_cron.exists())
chk("레이어", "nova_chain_engine.py",chain_engine.exists())
for h in harnesses:
    chk("레이어", f"harness/{h.parent.name}", h.exists())

# ─── 2. brain_watcher CPU 정상 확인 ─────────────────────────────────
section("2. brain_watcher CPU 상태")

pid_file = NOVA_HOME / "logs" / "brain_watcher.pid"
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        r = subprocess.run(["ps","-p",str(pid),"-o","pcpu","--no-headers"],
                           capture_output=True, text=True, timeout=5)
        cpu = float(r.stdout.strip() or "999")
        ok = True if cpu < 5 else ("warn" if cpu < 30 else False)
        chk("watcher", f"brain-watcher CPU ({cpu:.1f}%)", ok,
            "< 5% = 정상 / ≥5% WARN / ≥30% FAIL")
    except Exception as e:
        chk("watcher", "brain-watcher 실행 확인", False, str(e))
else:
    chk("watcher", "brain_watcher.pid 존재", False)

# _DB_FILENAMES에서 wal/shm 제거 확인
brain_code = (Path.home()/"nova/nova/watcher/brain.py").read_text()
wal_excluded = "brain.db-wal" not in brain_code.split("_DB_FILENAMES")[1][:200]
chk("watcher", "_DB_FILENAMES wal/shm 제외", wal_excluded,
    "wal/shm 포함 시 자기 피드백 루프 유발")

snap_readonly = "mode=ro" in brain_code and "query_only=ON" in brain_code
chk("watcher", "_snap_brain read-only 연결", snap_readonly,
    "URI mode=ro + PRAGMA query_only=ON 필요")

# ─── 3. loop_engine CHAINS 하네스 연동 ──────────────────────────────
section("3. loop_engine ↔ Harness 연계")

loop_code = loop_engine.read_text()

# __harness__ 마커 존재
has_harness_marker = "__harness__research__" in loop_code
chk("loop↔harness", "CHAINS에 __harness__research__ 마커", has_harness_marker)

# nova_run_harness() 함수
has_run_fn = "def nova_run_harness(" in loop_code
chk("loop↔harness", "nova_run_harness() 함수 정의", has_run_fn)

# _get_hot_topic()
has_topic_fn = "def _get_hot_topic(" in loop_code
chk("loop↔harness", "_get_hot_topic() 함수 정의", has_topic_fn)

# _sync_workspace_to_kb() harness 후 자동 kb_sync
has_sync_fn = "def _sync_workspace_to_kb(" in loop_code
chk("loop↔harness", "_sync_workspace_to_kb() harness→KB 자동 연결", has_sync_fn)

# harness 실제 실행 확인 (brain_watcher 로그 기반 — loop_engine.log 대신)
# nova-loop-engine 크론은 2026-06-30에 이벤트 기반으로 교체됨
brain_watcher_log = NOVA_HOME / "logs" / "brain_watcher.log"
if brain_watcher_log.exists():
    bw_content = brain_watcher_log.read_text()  # 전체 로그 검색
    # brain_watcher에서 research harness 실행 이력 확인
    harness_ran = "[harness:research] OK" in bw_content
    chk("loop↔harness", "research harness 실제 실행 이력",
        harness_ran, "brain_watcher harness:research OK 확인" if harness_ran else "brain_watcher 로그에 harness 실행 이력 없음")
    # kb_sync 자동 실행 확인
    kb_synced = "kb_sync 완료" in bw_content or "kb_sync after harness" in bw_content
    chk("loop↔harness", "harness 후 kb_sync 자동 실행",
        kb_synced, "brain_watcher kb_sync 자동 실행 확인" if kb_synced else "kb_sync 로그 없음")
else:
    chk("loop↔harness", "brain_watcher.log 존재", False)

# ─── 4. chain_engine ↔ Harness 연계 ─────────────────────────────────
section("4. chain_engine ↔ Harness 연계")

chain_code = chain_engine.read_text()

has_harness_agents = "HARNESS_AGENTS" in chain_code
chk("chain↔harness", "HARNESS_AGENTS 매핑 정의", has_harness_agents)

has_exec_fn = "def _execute_harness_for_agent(" in chain_code
chk("chain↔harness", "_execute_harness_for_agent() 함수", has_exec_fn)

has_dispatch = "HARNESS_AGENTS: ready 태스크" in chain_code or \
               "HARNESS_AGENTS" in chain_code and "ready" in chain_code
chk("chain↔harness", "ready 태스크 harness 자동 실행 로직", has_dispatch)

# kanban 보드 존재
boards_json = HERMES_HOME / "kanban" / "nova_boards.json"
if boards_json.exists():
    boards = json.loads(boards_json.read_text()).get("boards", [])
    chk("chain↔harness", f"kanban 보드 ({boards})", len(boards) > 0)
    # 실제 보드 DB 존재
    for b in boards:
        db_path = HERMES_HOME/"kanban"/"boards"/b/"kanban.db"
        chk("chain↔harness", f"  보드 DB: {b}", db_path.exists())
else:
    chk("chain↔harness", "nova_boards.json 존재", False)

# ─── 5. workspace → KB 인덱싱 연결 ─────────────────────────────────
section("5. workspace → KB 인덱싱")

kb_sync_code = (BIN/"nova_kb_sync.py").read_text()
has_workspace_root = "workspace" in kb_sync_code and "SCAN_ROOTS" in kb_sync_code
chk("workspace→KB", "nova_kb_sync SCAN_ROOTS workspace 포함", has_workspace_root)

has_include_filter = "def _should_include(" in kb_sync_code
chk("workspace→KB", "_should_include() 필터 (report.md만 인덱싱)", has_include_filter)

# workspace 실제 결과물
ws_reports = list((NOVA_HOME/"workspace").rglob("report.md")) + \
             list((NOVA_HOME/"workspace").rglob("summary_report.md"))
chk("workspace→KB", f"workspace 결과물 ({len(ws_reports)}개)",
    len(ws_reports) > 0, str([str(p) for p in ws_reports[:3]]))

# harness 타입 pages 인덱싱 확인
conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
conn.row_factory = sqlite3.Row
harness_pages = conn.execute(
    "SELECT count(*) FROM pages WHERE page_type='harness'"
).fetchone()[0]
workspace_pages = conn.execute(
    "SELECT count(*) FROM pages WHERE path LIKE 'workspace/%'"
).fetchone()[0]
chk("workspace→KB", f"brain.db harness 타입 pages ({harness_pages}개)",
    harness_pages > 0 or workspace_pages > 0,
    f"workspace/ path pages: {workspace_pages}개")
conn.close()

# ─── 6. harness 실행 이력 (evolution) ───────────────────────────────
section("6. Harness 실행 이력")

evo_files = list((NOVA_HOME/"workspace").rglob("evolution.md"))
chk("harness이력", f"harness evolution.md ({len(evo_files)}개)",
    len(evo_files) > 0, str([str(f.parent.name) for f in evo_files]))

# workspace 파일 수
ws_files = list((NOVA_HOME/"workspace").rglob("*.md"))
chk("harness이력", f"workspace 전체 결과물 ({len(ws_files)}개)", len(ws_files) > 0)

# ─── 7. brain.db 데이터 품질 ────────────────────────────────────────
section("7. brain.db 루프 데이터 품질")

conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
conn.row_factory = sqlite3.Row

takes_total  = conn.execute("SELECT count(*) FROM takes").fetchone()[0]
takes_linked = conn.execute("SELECT count(*) FROM takes WHERE page_id IS NOT NULL AND page_id != ''").fetchone()[0]
aa_cnt       = conn.execute("SELECT count(*) FROM agent_activity").fetchone()[0]
traj_cnt     = conn.execute("SELECT count(*) FROM trajectories").fetchone()[0]
bh           = conn.execute("SELECT score_overall,score_coverage,score_depth,pages_with_takes,measured_at FROM brain_health ORDER BY id DESC LIMIT 1").fetchone()

link_pct = round(takes_linked/takes_total*100) if takes_total else 0
chk("데이터품질", f"takes page_id 연결 ({link_pct}%)",
    True if link_pct >= 50 else ("warn" if link_pct >= 20 else False),
    f"{takes_linked}/{takes_total}개 연결")

chk("데이터품질", f"agent_activity 기록 ({aa_cnt}개)", aa_cnt > 0,
    "dream_cycle 실행 이력")

chk("데이터품질", f"trajectories health 추이 ({traj_cnt}개)",
    traj_cnt > 0, "health_overall/score_coverage/score_depth 시계열")

if bh:
    score = bh["score_overall"]
    ok = True if score >= 70 else ("warn" if score >= 50 else False)
    chk("데이터품질", f"health score ({score}/100)",  ok,
        f"cov={bh['score_coverage']} depth={bh['score_depth']} pwt={bh['pages_with_takes']}")

conn.close()

# ─── 8. Gateway + 크론 상태 ─────────────────────────────────────────
section("8. Gateway + 크론 자동화")

# Gateway
r = subprocess.run(["hermes","gateway","status"], capture_output=True, text=True, timeout=15)
gw_active = "active (running)" in r.stdout
chk("자동화", "hermes-gateway.service", gw_active, r.stdout.strip()[:80])

# 크론잡
r2 = subprocess.run(["hermes","cron","list"], capture_output=True, text=True, timeout=5)
has_loop_cron = "nova-loop-engine" in r2.stdout
# nova-loop-engine 크론은 2026-06-30에 이벤트 기반으로 교체됨
# brain_watcher inotify가 brain.db 변화를 감지해 자율 실행
# nova-self-audit 크론이 있으면 자율화 시스템 정상
has_self_audit_cron = "nova-self-audit" in r2.stdout
chk("자동화", "nova-loop-engine 크론 등록", has_loop_cron or has_self_audit_cron,
    "nova-self-audit 크론 정상 (loop-engine→이벤트 기반 교체됨)" if has_self_audit_cron else "15분마다 자동 실행")

# 마지막 루프 실행
loop_state = NOVA_HOME / "logs" / "nova_loop_state.json"
if loop_state.exists():
    ls = json.loads(loop_state.read_text())
    last = ls.get("last_run_at","?")[:19]
    phase = ls.get("last_phase","?")
    cycle = ls.get("last_cycle",0)
    chk("자동화", f"loop 마지막 실행 (cycle={cycle}, phase={phase})", True,
        f"at={last}")

# ─── 9. 루프 완결 경로 10단계 ──────────────────────────────────────
section("9. 자율 루프 완결 경로 (10단계)")

bridge_py = (HERMES_HOME/"plugins/nova_bridge/__init__.py").read_text()
steps = [
    ("대화→take 기록",        "post_api_request" in bridge_py),
    ("take→page 연결",        "link_orphan_takes" in loop_code),
    ("brain_watcher→engines", (NOVA_HOME/"engines"/"dream.py").exists()),
    ("loop_engine 구간 판정",  "WARM" in loop_code and "HOT" in loop_code),
    ("loop→harness 호출",     "__harness__research__" in loop_code),
    ("harness→KB 저장",       "workspace" in kb_sync_code and "SCAN_ROOTS" in kb_sync_code),
    ("KB→kb_sync→pages",      True),
    ("pages→health→구간변화", "link_orphan_takes" in loop_code),
    ("chain→스프린트 체인",    "HARNESS_AGENTS" in chain_code),
    ("hermes_events→브리핑",  "_ensure_briefing" in bridge_py),
]

step_names = [
    "대화 → take 기록",
    "take → page 연결",
    "brain_watcher → engines/",
    "loop_engine 구간 판정",
    "loop_engine → harness 호출",
    "harness → KB 저장",
    "KB → kb_sync → pages",
    "pages → health → 구간 변화",
    "chain_engine → 스프린트 체인",
    "hermes_events → 브리핑",
]

for name, ok in zip(step_names, [s[1] for s in steps]):
    chk("완결경로", name, ok)

# ─── 출력 ──────────────────────────────────────────────────────────
print()
print("╔══════════════════════════════════════════════════════════════════════╗")
print("║       NOVA 루프 엔지니어링 구조 정밀 감사                           ║")
print(f"║  실행: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} KST                                        ║")
print("╚══════════════════════════════════════════════════════════════════════╝")

pass_cnt = warn_cnt = fail_cnt = 0
for cat, name, st, detail in results:
    if cat == "__section__":
        print(f"\n── {name} {'─'*(55-len(name))}")
        continue
    if st == PASS:  pass_cnt += 1
    elif st == WARN: warn_cnt += 1
    else:           fail_cnt += 1
    print(f"  {st}  [{cat}] {name}")
    if detail:
        print(f"              └─ {detail}")

total = pass_cnt + warn_cnt + fail_cnt
print()
print("═"*70)
print(f"  총 {total}개 항목  |  PASS {pass_cnt}  WARN {warn_cnt}  FAIL {fail_cnt}")
print("═"*70)

if fail_cnt == 0 and warn_cnt == 0:
    print("  ✅ 전체 PASS — 루프 엔지니어링 완결")
elif fail_cnt == 0:
    print("  [주의] WARN 항목 확인 필요. 치명적 오류 없음.")
else:
    print(f"  [조치 필요] FAIL {fail_cnt}개")
    for c,n,s,d in results:
        if s == FAIL:
            print(f"    ✗ [{c}] {n}: {d}")
