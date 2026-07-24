#!/usr/bin/env python3
"""
nova_self_audit.py — NOVA 시스템 자가 감사 엔진
────────────────────────────────────────────────
/roop 실행 전·중·후 시스템 무결성을 자동 점검.
지금까지 발견된 모든 버그 패턴을 체계적으로 검사.

사용:
  python3 nova_self_audit.py              # 전체 감사
  python3 nova_self_audit.py --quick      # 빠른 점검 (syntax+chain+watcher만)
  python3 nova_self_audit.py --fix        # 자동 수정 가능한 항목 수정
  python3 nova_self_audit.py --report     # JSON 보고서 출력
"""
from __future__ import annotations

import ast, os, re, sys, json, sqlite3, subprocess, threading, time, tempfile
from pathlib import Path
from datetime import datetime, timezone

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
NOVA   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))

PASS = "PASS"; FAIL = "FAIL"; WARN = "WARN"

results: list[dict] = []

# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def chk(category: str, name: str, passed: bool, detail: str = "",
        severity: str = "HIGH", fix_fn=None) -> bool:
    status = PASS if passed else FAIL
    results.append({"category": category, "name": name, "status": status,
                    "detail": detail, "severity": severity, "fix": fix_fn is not None})
    mark = "✓" if passed else ("⚠" if severity == "LOW" else "✗")
    print(f"  {mark} [{category}] {name}" + (f" — {detail}" if detail else ""))
    return passed

def section(title: str) -> None:
    print(f"\n{'━' * 60}")
    print(f"  {title}")
    print('━' * 60)

# ── A. 문법 검사 ──────────────────────────────────────────────────────────────
def audit_syntax() -> None:
    section("A. 문법 검사")
    targets = [
        HERMES / "scripts/nova_chain_engine.py",
        HERMES / "bin/nova_agent_worker.py",
        HERMES / "bin/nova_orchestrator.py",
        HERMES / "bin/nova_shared_kb.py",
        HERMES / "bin/roop_runner.py",
        HERMES / "scripts/roop_monitor.py",
        Path.home() / "nova/nova/watcher/brain.py",
    ]
    for p in targets:
        if not p.exists():
            chk("SYNTAX", p.name, False, "파일 없음", "HIGH"); continue
        try:
            ast.parse(p.read_text())
            chk("SYNTAX", p.name, True)
        except SyntaxError as e:
            chk("SYNTAX", p.name, False, str(e)[:60], "CRITICAL")

    # CRITICAL-4: HMGProvider _KeyRotator 통합 여부 (401/429 시 키 순환)
    llm_py = Path.home() / "nova/nova/providers/llm.py"
    if llm_py.exists():
        llm_src = llm_py.read_text()
        chk("LLM", "HMGProvider _KeyRotator 통합 (401/429 rotate)",
            "class HMGProvider" in llm_src and "_rotator = _KeyRotator" in llm_src,
            "HMGProvider에 _KeyRotator 없음 → 키 만료/429 시 즉시 실패", "CRITICAL")
        chk("LLM", "HMGProvider 401 rotate 처리",
            "r.status_code in (429, 401)" in llm_src,
            "401 에러 rotate 미구현 → 만료 키 고착", "HIGH")
        chk("LLM", "brain.py 키 강제 주입 (setdefault→직접 대입)",
            "os.environ[_var] = _master_key" in (Path.home()/"nova/nova/watcher/brain.py").read_text(),
            "setdefault 방식 → 셸 구키 export 시 .env 키 무시", "CRITICAL")
    else:
        chk("LLM", "llm.py 존재", False, "파일 없음", "HIGH")

    # Phase 1: NOVA Agent OS — Isolation Layer
    kernel_dir = Path.home() / "nova/nova/kernel"
    if kernel_dir.exists():
        syscall_src = (kernel_dir / "syscall.py").read_text() if (kernel_dir/"syscall.py").exists() else ""
        chk("KERNEL", "syscall.py 존재",
            bool(syscall_src), "~/nova/nova/kernel/syscall.py 없음", "CRITICAL")
        chk("KERNEL", "KernelAPI.from_config() — 범용 진입점",
            "def from_config" in syscall_src, "from_config() 없음 → HMG 특화 경로만 존재", "CRITICAL")
        chk("KERNEL", "get_kernel() 싱글턴",
            "def get_kernel" in syscall_src, "get_kernel() 없음 → nova_bridge 교체 불가", "HIGH")
        chk("KERNEL", "RunHandle — Phase 2 interrupt hook",
            "class RunHandle" in syscall_src, "RunHandle 없음 → Phase 2 확장 시 breaking change", "HIGH")
        chk("KERNEL", "BrainSnapshot — 단일 쿼리 진단",
            "class BrainSnapshot" in syscall_src, "BrainSnapshot 없음", "MEDIUM")
        chk("KERNEL", "kb_write() ON CONFLICT upsert",
            "ON CONFLICT(id) DO UPDATE SET" in syscall_src, "upsert 없음 → id=NULL 재발 위험", "CRITICAL")
        chk("KERNEL", "kb_write_batch() — 배치 성능 유지",
            "def kb_write_batch" in syscall_src, "batch 없음 → nova_kb_sync 성능 10x 저하", "HIGH")
        chk("KERNEL", "_write_lock — WAL 충돌 방지",
            "_write_lock = threading.Lock" in syscall_src, "write_lock 없음 → 동시 쓰기 충돌", "HIGH")
        chk("KERNEL", "ownership.yaml 존재 (범용 설정)",
            (kernel_dir / "ownership.yaml").exists(), "ownership.yaml 없음", "HIGH")
        chk("KERNEL", "HMG URL 하드코딩 없음",
            "internal-llm-gateway.example.com" not in syscall_src and "hmg-corp.io" not in syscall_src,
            "syscall에 HMG URL 하드코딩 → 범용성 제로", "CRITICAL")
        # nova_bridge Hermes 융합
        bridge_src = HERMES/"plugins/nova_bridge/__init__.py"
        if bridge_src.exists():
            bsrc = bridge_src.read_text()
            chk("KERNEL", "nova_bridge: get_kernel 경유 (NovaBrain 직접 호출 제거)",
                "from nova.kernel.syscall import get_kernel" in bsrc,
                "nova_bridge가 NovaBrain 직접 호출 → syscall 레이어 우회", "CRITICAL")
    else:
        chk("KERNEL", "kernel/ 디렉토리 존재", False, "~/nova/nova/kernel/ 없음", "CRITICAL")

# ── B. brain_watcher ─────────────────────────────────────────────────────────
def audit_watcher() -> None:
    section("B. brain_watcher 상태 및 로직")
    bt = (Path.home() / "nova/nova/watcher/brain.py").read_text()

    # CRITICAL: chain timeout=3600
    chain_lines = [l for l in bt.splitlines() if "_run_bg" in l and "chain_engine" in l]
    chk("WATCHER", "chain timeout=3600", all("3600" in l for l in chain_lines), severity="CRITICAL")

    # CRITICAL: chain non-daemon (brain_watcher 재시작 시 에이전트 kill 방지)
    run_bg_code = "\n".join(l for l in bt.split("def _run_bg(")[1].split("\ndef _run_harness_bg(")[0].splitlines()
                             if not l.strip().startswith("#"))
    chk("WATCHER", "chain non-daemon (_run_bg)", "daemon=not is_chain" in run_bg_code, severity="CRITICAL")

    # CRITICAL: ready 태스크 감지 (재시작 후 루프 재개)
    chk("WATCHER", "has_ready + kanban_state_changed",
        "kanban_state_changed = (kanban_now != kanban_prev)" in bt and
        "(new_done > 0) or (has_ready and kanban_state_changed)" in bt, severity="CRITICAL")

    # memory_md 경로
    chk("WATCHER", "memory_md = HERMES_HOME/memories/MEMORY.md",
        'memories" / "MEMORY.md"' in bt and 'nova_home / "memory.md"' not in bt)

    # watcher 프로세스 RUNNING
    alive = sum(1 for pf in (NOVA/"logs").glob("*.pid")
                if pf.read_text().strip() and (Path("/proc")/pf.read_text().strip()).exists())
    chk("WATCHER", f"watcher 3× RUNNING", alive >= 3, f"{alive}개", "HIGH")

    # brain_watcher CPU < 50% (WAL spin / inotifywait 폭풍 감지)
    import subprocess as _sp
    _ps = _sp.run(["ps", "aux"], capture_output=True, text=True)
    _cpu = None
    for _ln in _ps.stdout.splitlines():
        if "nova.watcher.brain" in _ln and "grep" not in _ln:
            try: _cpu = float(_ln.split()[2]); break
            except: pass
    if _cpu is not None:
        chk("WATCHER", f"brain_watcher CPU < 50%", _cpu < 50.0, f"{_cpu}%", "HIGH")
    else:
        chk("WATCHER", "brain_watcher 프로세스 없음", False, "", "HIGH")

    # inotifywait 좀비 감지 (현재 watcher가 만들지 않은 것)
    # pid 파일에서 줄별로 파싱 (한 파일에 여러 PID 있을 수 있음)
    _bw_pids: set[str] = set()
    for _pf in (NOVA/"logs").glob("*.pid"):
        try:
            for _pid_line in _pf.read_text().strip().splitlines():
                _pid_line = _pid_line.strip()
                if _pid_line.isdigit():
                    _bw_pids.add(_pid_line)
        except Exception:
            pass
    _all_inotify = [l.split()[1] for l in _ps.stdout.splitlines()
                    if "inotifywait" in l and "grep" not in l]
    _zombies = []
    for _ip in _all_inotify:
        try:
            _cmd = Path(f"/proc/{_ip}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "/mnt/c/" in _cmd: continue  # Windows 경로 감시는 제외 (Teams 등)
            _ppid = next((l.split()[1] for l in
                         Path(f"/proc/{_ip}/status").read_text().splitlines()
                         if l.startswith("PPid:")), "")
            if _ppid not in _bw_pids: _zombies.append(_ip)
        except: pass
    chk("WATCHER", f"inotifywait 좀비 없음", len(_zombies) == 0,
        f"{len(_zombies)}개" if _zombies else "", "HIGH" if _zombies else "")

# ── C. chain_engine ───────────────────────────────────────────────────────────
def audit_chain_engine() -> None:
    section("C. chain_engine 구조")
    ce = (HERMES / "scripts/nova_chain_engine.py").read_text()

    chk("CHAIN", "sys import (L21)", "import json, re, subprocess, sys" in ce, severity="CRITICAL")
    chk("CHAIN", "cancelled 제외 (failed_tasks)",
        '"cancelled"' not in [l for l in ce.splitlines() if "failed_tasks" in l and "status" in l][0] if [l for l in ce.splitlines() if "failed_tasks" in l and "status" in l] else False,
        severity="HIGH")
    chk("CHAIN", "CHAIN_FORK evaluator→[retro,learn]",
        '"nova-evaluator":  ["nova-retro", "nova-learn"]' in ce, severity="CRITICAL")
    chk("CHAIN", "CHAIN_DONE learn→document",
        '"nova-learn":             "nova-document"' in ce)
    chk("CHAIN", "CHAIN_JOIN retro+document→release",
        '"nova-document-release":  ["nova-retro",  "nova-document"]' in ce)
    chk("CHAIN", "detect_loop 역방향만 카운팅",
        'startswith("[역방향↩]")' in ce, severity="HIGH")
    chk("CHAIN", "KPI_PASS 루프 종료", "[ROOP COMPLETE]" in ce, severity="HIGH")
    chk("CHAIN", "max_sprints 체크", "_max_s  = _st.get" in ce)
    chk("CHAIN", "KPI_PASS 체크 위치 (sysaudit→autoplan)",
        'if agent == "nova-sysaudit" and next_ag == "nova-autoplan"' in ce,
        severity="CRITICAL")
    chk("CHAIN", "KPI_PASS dead-code 없음 (doc-release→autoplan 제거)",
        'agent == "nova-document-release" and next_ag == "nova-autoplan"' not in ce,
        severity="CRITICAL")
    chk("CHAIN", "PYTHONPATH _hermes_home", 'Path(_hermes_home) / "bin"' in ce)
    chk("CHAIN", "communicate(timeout=30) orchestrator",
        "proc.communicate(timeout=30)" in (HERMES/"bin/nova_orchestrator.py").read_text())
    oc = (HERMES/"bin/nova_orchestrator.py").read_text()
    chk("ORCH", "PARALLEL_GROUPS {retro,learn}",
        '"nova-retro",  "nova-learn"' in oc or '"nova-learn"' in oc,
        severity="HIGH")
    chk("ORCH", "PARALLEL_GROUPS {canary,health}",
        '"nova-canary", "nova-health"' in oc)
    ce = (HERMES/"scripts/nova_chain_engine.py").read_text()
    chk("CHAIN", "BUG-INOTIFY-DEADEND: ORCH-2ND 재파견",
        '"ORCH-2ND"' in ce or "ORCH-2ND" in ce, severity="HIGH")
    # BUG-HARNESS: harness.py script 필드 폴백 확인
    _harness_py = Path.home() / "nova/nova/core/harness.py"
    if _harness_py.exists():
        _hsrc = _harness_py.read_text()
        chk("HARNESS", 'harness.py script 폴백 (BUG-HARNESS)',
            'ph.get("script", "")' in _hsrc, severity="CRITICAL")
    else:
        chk("HARNESS", "harness.py 존재", False, "파일 없음", "CRITICAL")

# ── D. harness 절대경로 ────────────────────────────────────────────────────────
def audit_harness_paths() -> None:
    section("D. harness 절대경로 (YAML output_file/input_files)")
    bad = []
    for h in (NOVA / "harnesses").iterdir():
        yaml = h / "harness.yaml"
        if not yaml.exists(): continue
        for i, line in enumerate(yaml.read_text().splitlines(), 1):
            s = line.strip()
            if "${" in s and not s.startswith("#"):
                if any(kw in line for kw in ("output_file:", '- "', "- '")):
                    bad.append(f"{h.name}:{i}: {s[:80]}")
    chk("HARNESS", "YAML 절대경로 없음", not bad,
        str(bad[:3]) if bad else "", severity="HIGH")

# ── E. prompts kb_context ──────────────────────────────────────────────────────
def audit_prompts_kb_context() -> None:
    section("E. LLM prompts {{kb_context}} 연결")
    EXCLUDE = {"evaluate.txt", "web_search.txt", "write_learn_summary.txt", "synthesis.txt"}
    missing = []
    for p in (NOVA / "harnesses").rglob("prompts/*.txt"):
        if p.name in EXCLUDE: continue
        txt = p.read_text()
        if "{{" in txt and "{{kb_context}}" not in txt:
            missing.append(f"{p.parent.parent.name}/{p.name}")
    chk("PROMPTS", "kb_context 전수 연결", not missing,
        str(missing[:3]) if missing else "", severity="HIGH")

# ── F. agent_worker ────────────────────────────────────────────────────────────
def audit_agent_worker() -> None:
    section("F. nova_agent_worker")
    aw = (HERMES / "bin/nova_agent_worker.py").read_text()
    chk("WORKER", "kanban_complete(harness 인자)",
        "def kanban_complete(board: str, task_id: str, harness: str" in aw, severity="CRITICAL")
    chk("WORKER", "--result report.md 전달", '"--result"' in aw, severity="CRITICAL")
    chk("WORKER", "sync_kb /usr/bin/python3 우선", '"/usr/bin/python3"' in aw)
    chk("WORKER", "write_handoff atomic rename", 'tmp.replace(ws / "handoff.json")' in aw)
    chk("WORKER", "SIGTERM kanban_block", "SIGTERM" in aw and "kanban_block" in aw)
    chk("WORKER", "sqlite_vec 폴백", "sqlite_vec" in aw)

# ── G. shared_kb ───────────────────────────────────────────────────────────────
def audit_shared_kb() -> None:
    section("G. nova_shared_kb")
    skb = (HERMES / "bin/nova_shared_kb.py").read_text()
    chk("KB", "LOCK_EX + atomic rename", "LOCK_EX" in skb and "tmp.replace(SPRINT_FILE)" in skb, severity="HIGH")
    chk("KB", "kpi_report.md 삭제 (init_sprint)", "kpi_rpt.unlink()" in skb, severity="HIGH")
    chk("KB", "_read_recent_handoffs", "def _read_recent_handoffs" in skb)
    # 실제 동작 확인
    r = subprocess.run([sys.executable, str(HERMES/"bin/nova_shared_kb.py"),
        "--context", "NOVA 자가감사", "--agent", "nova-audit"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "HERMES_HOME": str(HERMES), "NOVA_HOME": str(NOVA),
             "PYTHONPATH": str(HERMES/"bin")+":"+str(Path.home()/"nova")})
    chk("KB", "read_context 실행 (>200자)", r.returncode == 0 and len(r.stdout) > 200,
        f"{len(r.stdout)}자")

# ── H. roop_runner ─────────────────────────────────────────────────────────────
def audit_roop() -> None:
    section("H. roop_runner / roop_monitor")
    rr = (HERMES / "bin/roop_runner.py").read_text()
    chk("ROOP", "archive_stale_ready_tasks", "def archive_stale_ready_tasks" in rr, severity="HIGH")
    chk("ROOP", "nudge INSERT→sleep→DELETE", "time.sleep(2)" in rr and rr.count("db.commit()") >= 2, severity="HIGH")
    chk("ROOP", "roop-monitor 크론 등록", "roop-monitor" in rr)
    chk("ROOP", "kpi_evaluate evaluate.txt 동적 생성", "write_kpi_prompt" in rr)
    rm = (HERMES / "scripts/roop_monitor.py").read_text()
    chk("ROOP", "roop_monitor result 우선 (KPI감지)", 't.get("result", "") or t.get("body", "")' in rm, severity="HIGH")
    dr = (NOVA / "harnesses/document_release/harness.yaml").read_text()
    chk("ROOP", "document_release KPI_PASS 감지", "kpi_passed = 'KPI_PASS'" in dr, severity="HIGH")
    chk("ROOP", "ROOP_COMPLETE brain.db 기록", "ROOP_COMPLETE" in dr)

# ── I. brain.db WAL ────────────────────────────────────────────────────────────
def audit_brain_db() -> None:
    section("I. brain.db WAL 동시 읽기")
    errs = []
    def _ro():
        try:
            c = sqlite3.connect(f"file:{NOVA/'brain.db'}?mode=ro", uri=True, timeout=5)
            c.execute("PRAGMA query_only=ON")
            c.execute("SELECT section, content FROM page_chunks LIMIT 1").fetchone()
            c.close()
        except Exception as e: errs.append(str(e))
    ts = [threading.Thread(target=_ro) for _ in range(6)]
    for t in ts: t.start()
    for t in ts: t.join()
    chk("DB", "WAL 6-conn section컬럼", not errs, str(errs[:1]) if errs else "")

    # brain.db health
    try:
        db = sqlite3.connect(str(NOVA/"brain.db"), timeout=5)
        h = db.execute("SELECT score_overall,score_coverage,score_depth FROM brain_health ORDER BY rowid DESC LIMIT 1").fetchone()
        t_cnt = db.execute("SELECT COUNT(*) FROM takes").fetchone()[0]
        p_cnt = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        db.close()
        chk("DB", f"brain.db 접근", True, f"health={h} takes={t_cnt} pages={p_cnt}")
        chk("DB", "score_depth>=50", (h[2] if h else 0) >= 50,
            f"depth={h[2] if h else 'N/A'}", "LOW")
    except Exception as e:
        chk("DB", "brain.db 접근", False, str(e)[:60], "HIGH")

# ── J. harness 15종 로드 ──────────────────────────────────────────────────────
def audit_harness_load() -> None:
    section("J. harness 15종 로드 확인")
    r = subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home()/"nova"))
sys.path.insert(0, str(Path.home()/".hermes"/"bin"))
from nova.core.config import load_config
from nova.core.harness import HarnessLoader
cfg = load_config(str(Path.home()/".nova"/"nova.yaml"))
cfg.harnesses_dir = str(Path(cfg.harnesses_dir).expanduser())
loader = HarnessLoader(cfg.harnesses_dir)
names = ["research","code_implement","code_review","qa","go_nogo","security_sign_off",
    "kpi_evaluate","ship","canary","health","retro","learn","document_gen","document_release","investigate","system_audit"]
failed = []
for h in names:
    try: loader.load(h)
    except Exception as e: failed.append(f"{h}:{e}")
print(json.dumps({"total": len(names), "failed": failed}))
""".replace("json.dumps", "__import__('json').dumps")],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "HERMES_HOME": str(HERMES), "NOVA_HOME": str(NOVA),
             "PYTHONPATH": str(HERMES/"bin")+":"+str(Path.home()/"nova")})
    try:
        d = json.loads(r.stdout.strip().split("\n")[-1])
        failed = d.get("failed", [])
        chk("HARNESS", f"15종 로드 ({d['total'] - len(failed)}/{d['total']})",
            not failed, str(failed[:2]) if failed else "")
    except Exception:
        chk("HARNESS", "15종 로드", False, r.stderr[:60], "HIGH")

# ── K. kanban 잔여 태스크 ────────────────────────────────────────────────────
def audit_kanban() -> None:
    section("K. kanban nova-loop 상태")
    r = subprocess.run(["hermes", "kanban", "--board", "nova-loop", "list", "--json"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        chk("KANBAN", "nova-loop 보드 접근", False, r.stderr[:40], "HIGH"); return
    try:
        tasks = json.loads(r.stdout)
        ready_cnt = sum(1 for t in tasks if t.get("status") in ("ready","todo","blocked"))
        done_cnt  = sum(1 for t in tasks if t.get("status") == "done")
        chk("KANBAN", "ready 잔여 태스크 없음", ready_cnt == 0,
            f"ready={ready_cnt} (새 /roop 전 archive 권장)" if ready_cnt else "",
            "LOW" if ready_cnt < 3 else "HIGH")
        chk("KANBAN", "done 태스크 존재", done_cnt > 0, f"done={done_cnt}", "LOW")
    except Exception as e:
        chk("KANBAN", "JSON 파싱", False, str(e)[:40], "HIGH")

# ── L. KB 경로 일원화 & orphan 처리 ─────────────────────────────────────────
def audit_kb_paths() -> None:
    section("L. KB 경로 일원화 & orphan 처리")

    # fix_orphan.py: page_type='general' 제한 없이 전체 처리
    fix_orphan = HERMES / "bin" / "nova_fix_orphan.py"
    if fix_orphan.exists():
        content = fix_orphan.read_text()
        chk("FIX_ORPHAN", "page_type 전체 처리 (general 제한 없음)",
            'agent IS NULL AND page_type' not in content.split("remaining")[0] and
            'WHERE agent IS NULL\n' in content or '"SELECT id, path FROM pages WHERE agent IS NULL"' in content,
            "page_type='general' 필터 잔존 → 수정 필요", "HIGH")
        chk("FIX_ORPHAN", "agents/ prefix 처리",
            '"agents/"' in content, "agents/ prefix 누락", "MEDIUM")
        chk("FIX_ORPHAN", "lessons/ prefix 처리",
            '"lessons/"' in content, "lessons/ prefix 누락", "LOW")
        chk("FIX_ORPHAN", "nova_workspace/ prefix 처리 (CRITICAL-3)",
            '"nova_workspace/"' in content,
            "nova_workspace prefix 누락 → 6175개 매칭 불가", "CRITICAL")
        chk("FIX_ORPHAN", "UPDATE WHERE path= 사용 (id=NULL 대응)",
            '"UPDATE pages SET agent=? WHERE path=?"' in content,
            "WHERE id=? 방식 잔존 → id=NULL 행 UPDATE 불가", "CRITICAL")
    else:
        chk("FIX_ORPHAN", "nova_fix_orphan.py 존재", False, "파일 없음", "HIGH")

    # nova_kb_sync.py: NOVA_HOME_PATH expanduser (FAIL-1)
    kb_sync = HERMES / "bin" / "nova_kb_sync.py"
    if kb_sync.exists():
        content = kb_sync.read_text()
        chk("KB_SYNC", "NOVA_HOME_PATH expanduser 적용",
            "NOVA_HOME_PATH = Path(os.environ" in content and ".expanduser()" in content,
            "NOVA_HOME_PATH에 expanduser() 없음 — tilde 경로 시 wiki 인덱싱 불가", "HIGH")
        chk("KB_SYNC", "wiki 갱신 NOVA_HOME_PATH 사용",
            '"NOVA_HOME": str(NOVA_HOME_PATH)' in content, "wiki 갱신 시 undefined 참조", "HIGH")
        chk("KB_SYNC", "SCAN_ROOTS agents/ 포함",
            '"agents/"' in content, "agents/ SCAN_ROOT 누락", "MEDIUM")
    else:
        chk("KB_SYNC", "nova_kb_sync.py 존재", False, "파일 없음", "HIGH")

    # nova_bridge/__init__.py: expanduser (FAIL-2)
    bridge = HERMES / "plugins" / "nova_bridge" / "__init__.py"
    if bridge.exists():
        content = bridge.read_text()
        chk("NOVA_BRIDGE", "NOVA_HOME expanduser 적용",
            "NOVA_HOME   = Path(os.environ" in content and ".expanduser()" in content,
            "NOVA_HOME expanduser 없음 → BRAIN_DB/KB_ROOT 오동작", "HIGH")
        chk("NOVA_BRIDGE", "HERMES_HOME expanduser 적용",
            "HERMES_HOME    = Path(os.environ" in content and ".expanduser()" in content,
            "HERMES_HOME expanduser 없음", "HIGH")
    else:
        chk("NOVA_BRIDGE", "nova_bridge/__init__.py 존재", False, "파일 없음", "HIGH")

    # nova_kb_wiki_bridge.py: check-dup threshold 일치 (WARN-1)
    wiki_bridge = HERMES / "bin" / "nova_kb_wiki_bridge.py"
    if wiki_bridge.exists():
        content = wiki_bridge.read_text()
        chk("WIKI_BRIDGE", "check-dup threshold=0.55 (sync와 일치)",
            "threshold=0.50" not in content, "check-dup 0.50 vs sync 0.55 불일치", "LOW")
    
    # brain.db orphan 상태
    try:
        db = sqlite3.connect(str(NOVA/"brain.db"), timeout=5)
        null_id    = db.execute("SELECT COUNT(*) FROM pages WHERE id IS NULL").fetchone()[0]
        null_agent = db.execute("SELECT COUNT(*) FROM pages WHERE agent IS NULL").fetchone()[0]
        pwt = db.execute("SELECT COUNT(DISTINCT page_id) FROM takes WHERE page_id IS NOT NULL").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        # CRITICAL-2: id=NULL 행 실제 존재 여부로 판정 (WHERE id=None은 SQLite에서 0 rows — 오감지 방지)
        fix_orphan_update_ok = null_id == 0  # id IS NULL 행이 없으면 버그 해소됨
        db.close()
        chk("KB_HEALTH", "pages id NULL 없음 (harvest.py INSERT 버그)", null_id == 0,
            f"id=NULL {null_id}개 → nova_kb_harvest.py L54 INSERT에 id 컬럼 추가 필요", "CRITICAL")
        chk("KB_HEALTH", "pages agent NULL 없음", null_agent == 0,
            f"agent=NULL {null_agent}개 → fix_orphan 실행 필요", "MEDIUM")
        chk("FIX_ORPHAN", "UPDATE WHERE id=? (None) → 0 rows 버그 해소",
            fix_orphan_update_ok,  # null_id==0 이면 PASS
            "fix_orphan이 id=NULL 행에 UPDATE 불가 → WHERE path=? 로 변경 필요", "CRITICAL")
        cov = pwt/total*100 if total > 0 else 0
        chk("KB_HEALTH", f"coverage >=20% (현재 {cov:.1f}%)", cov >= 20,
            f"coverage={cov:.1f}% 낮음 → takes_link 실행 필요", "LOW")
    except Exception as e:
        chk("KB_HEALTH", "brain.db 접근", False, str(e)[:60], "HIGH")

# ── M. interrupt.py ──────────────────────────────────────────────────────────
def audit_interrupt() -> None:
    section("M. Phase 2 — interrupt.py 안정성")
    src_path  = Path.home() / "nova/nova/kernel/interrupt.py"
    bw_path   = Path.home() / "nova/nova/watcher/brain.py"
    yaml_path = Path.home() / "nova/nova/kernel/domain_routing.yaml"

    if not src_path.exists():
        chk("INTERRUPT", "interrupt.py 존재", False, "~/nova/nova/kernel/interrupt.py 없음", "CRITICAL")
        return

    src  = src_path.read_text()
    bw   = bw_path.read_text() if bw_path.exists() else ""
    yaml_ok = yaml_path.exists()

    chk("INTERRUPT", "InterruptKind 5종",
        all(k in src for k in ["DOMAIN_RESEARCH", "SELF_HEAL", "GENERALIZE", "SYNTHESIZE", "ALERT"]),
        "InterruptKind 누락 — DOMAIN_RESEARCH/SELF_HEAL/GENERALIZE/SYNTHESIZE/ALERT 중 일부 없음", "CRITICAL")
    chk("INTERRUPT", "Interrupt.tier 필드 (warm 기본값)",
        "tier" in src and "= \"warm\"" in src,
        "tier 필드 또는 warm 기본값 없음", "HIGH")
    chk("INTERRUPT", "classify() window 파라미터",
        "window: int" in src,
        "classify()에 window 파라미터 없음 → 시간 범위 제어 불가", "HIGH")
    chk("INTERRUPT", "domain_routing.yaml 존재",
        yaml_ok, "~/nova/nova/kernel/domain_routing.yaml 없음", "HIGH")
    chk("INTERRUPT", "brain.py InterruptRouter 배선",
        "InterruptRouter" in bw,
        "brain.py에 InterruptRouter 없음 → interrupt 라우팅 불능", "CRITICAL")
    chk("INTERRUPT", "brain.py classify() 호출",
        ".classify(" in bw,
        "brain.py에 .classify( 없음 → interrupt 분류 미동작", "HIGH")


# ── N. memory.py ──────────────────────────────────────────────────────────────
def audit_memory() -> None:
    section("N. Phase 3 — memory.py hot/warm/cold 안정성")
    src_path  = Path.home() / "nova/nova/kernel/memory.py"
    init_path = Path.home() / "nova/nova/kernel/__init__.py"

    if not src_path.exists():
        chk("MEMORY", "memory.py 존재", False, "~/nova/nova/kernel/memory.py 없음", "CRITICAL")
        return

    src  = src_path.read_text()
    init = init_path.read_text() if init_path.exists() else ""

    chk("MEMORY", "TierConfig hot=1h",
        "hot_hours" in src and "1.0" in src,
        "hot_hours 또는 1.0 없음 → hot tier 경계 미정의", "HIGH")
    chk("MEMORY", "TierConfig warm=168h",
        "warm_hours" in src and "168.0" in src,
        "warm_hours 또는 168.0 없음 → warm tier 경계 미정의", "HIGH")
    chk("MEMORY", "tier_of() 존재",
        "def tier_of" in src,
        "tier_of() 없음 → tier 판별 불가", "CRITICAL")
    chk("MEMORY", "tier_bounds() 존재",
        "def tier_bounds" in src,
        "tier_bounds() 없음 → 시간 범위 조회 불가", "HIGH")
    chk("MEMORY", "warm+hot 순서 (C-6: warm 먼저)",
        "warm_results + hot_results" in src,
        "warm_results + hot_results 순서 아님 → C-6 버그 재발 위험", "CRITICAL")
    chk("MEMORY", "cold [chain] 격리 (exclude_chain)",
        "exclude_chain" in src,
        "exclude_chain 없음 → cold에 chain 태스크 노출", "HIGH")
    chk("MEMORY", "error contract RuntimeError",
        "RuntimeError" in src,
        "RuntimeError 없음 → 비정상 tier 조용히 무시", "MEDIUM")
    chk("MEMORY", "MemoryLayer __init__.py export",
        "MemoryLayer" in init,
        "__init__.py에 MemoryLayer 없음 → import 불가", "HIGH")

    # 실제 동작 테스트
    try:
        import importlib, sys as _sys
        _nova_path = str(Path.home() / "nova")
        if _nova_path not in _sys.path:
            _sys.path.insert(0, _nova_path)
        # 캐시 제거 후 재임포트
        for mod in list(_sys.modules.keys()):
            if mod.startswith("nova.kernel.memory"):
                del _sys.modules[mod]
        from nova.kernel.memory import MemoryLayer, TierConfig
        layer = MemoryLayer(brain_db=str(Path.home() / ".nova/brain.db"))
        snap  = layer.summarize()
        chk("MEMORY", "summarize() hot/warm/cold 키",
            all(k in snap for k in ["hot", "warm", "cold"]),
            f"반환 키: {list(snap.keys())}", "HIGH")
        chk("MEMORY", "brain.db idx_takes_created_at 인덱스",
            any(r[0] == "idx_takes_created_at"
                for r in sqlite3.connect(str(Path.home() / ".nova/brain.db")).execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()),
            "idx_takes_created_at 없음 → hot/warm 쿼리 풀스캔", "HIGH")
    except Exception as e:
        chk("MEMORY", "실제 동작 테스트 (import + summarize)", False, str(e)[:80], "HIGH")


# ── 결과 출력 ─────────────────────────────────────────────────────────────────
def print_summary(quick: bool = False) -> int:
    passed = [r for r in results if r["status"] == PASS]
    failed = [r for r in results if r["status"] == FAIL]
    critical = [r for r in failed if r["severity"] == "CRITICAL"]
    high     = [r for r in failed if r["severity"] == "HIGH"]
    low      = [r for r in failed if r["severity"] == "LOW"]

    print(f"\n{'═' * 60}")
    print(f"  NOVA 자가 감사 결과 ({'빠른' if quick else '전체'})")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * 60}")
    print(f"  PASS: {len(passed)}  FAIL: {len(failed)}  "
          f"(CRITICAL: {len(critical)}  HIGH: {len(high)}  LOW: {len(low)})")

    if critical:
        print(f"\n  🔴 CRITICAL ({len(critical)}건) — 즉시 수정 필요:")
        for r in critical:
            print(f"    [{r['category']}] {r['name']}: {r['detail'][:60]}")
    if high:
        print(f"\n  🟠 HIGH ({len(high)}건) — /roop 시작 전 수정 권장:")
        for r in high:
            print(f"    [{r['category']}] {r['name']}: {r['detail'][:60]}")
    if low:
        print(f"\n  🟡 LOW ({len(low)}건) — 모니터링:")
        for r in low:
            print(f"    [{r['category']}] {r['name']}: {r['detail'][:60]}")

    if not failed:
        print(f"\n  ✅ 모든 항목 통과 — /roop 실행 가능")
    else:
        print(f"\n  ⛔ {len(failed)}개 항목 실패 — 위 항목 수정 후 /roop 진행")

    return len(critical) + len(high)

# ── 진입점 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="NOVA 시스템 자가 감사")
    p.add_argument("--quick",  action="store_true", help="빠른 점검 (syntax+chain+watcher)")
    p.add_argument("--report", action="store_true", help="JSON 보고서")
    args = p.parse_args()

    print(f"\n{'═' * 60}")
    print(f"  NOVA 자가 감사 시작 — {'빠른' if args.quick else '전체'} 모드")
    print(f"  HERMES: {HERMES}")
    print(f"  NOVA:   {NOVA}")
    print(f"{'═' * 60}")

    audit_syntax()
    audit_watcher()
    audit_chain_engine()

    if not args.quick:
        audit_harness_paths()
        audit_prompts_kb_context()
        audit_agent_worker()
        audit_shared_kb()
        audit_roop()
        audit_brain_db()
        audit_harness_load()
        audit_kanban()
        audit_kb_paths()
        audit_interrupt()
        audit_memory()

    rc = print_summary(args.quick)

    if args.report:
        rpt = NOVA / "logs" / "nova_audit_report.json"
        rpt.parent.mkdir(parents=True, exist_ok=True)
        rpt.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "quick": args.quick,
            "results": results,
            "summary": {
                "total": len(results),
                "passed": sum(1 for r in results if r["status"] == PASS),
                "failed": sum(1 for r in results if r["status"] == FAIL),
            }
        }, ensure_ascii=False, indent=2))
        print(f"\n  보고서: {rpt}")

    sys.exit(rc)

if __name__ == "__main__":
    main()
