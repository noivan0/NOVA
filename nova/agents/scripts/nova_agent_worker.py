#!/usr/bin/env python3
"""
nova_agent_worker.py — NOVA 독립 에이전트 워커 프로세스 v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

오케스트레이터(nova_orchestrator.py)가 subprocess.Popen()으로
이 스크립트를 에이전트별 독립 프로세스로 실행한다.

자체 kanban 처리 보장:
  완료 시 self → kanban complete/block 직접 처리
  오케스트레이터 생존 여부와 무관하게 동작.

공유 자원 (WAL 동시 접근):
  brain.db  → WAL 모드, busy_timeout=10s, 동시 읽기 OK
  KB 파일   → page_chunks BM25 읽기 전용
  MEMORY.md → 실시간 보조 읽기 전용
  handoff.json → workspace별 읽기/쓰기

실행:
  python3 nova_agent_worker.py \\
      --agent   nova-dev \\
      --harness code_implement \\
      --task-id t_abc123 \\
      --board   nova-loop \\
      --context '{"topic":"구현 주제"}'
"""
from __future__ import annotations

import os, sys, json, uuid, sqlite3, time, argparse, signal, subprocess, traceback
from pathlib import Path
from datetime import datetime, timezone

# ── 환경 ─────────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
BRAIN_DB    = NOVA_HOME / "brain.db"
KB_ROOT     = HERMES_HOME / "kb"
MEMORY_FILE = HERMES_HOME / "memories" / "MEMORY.md"

# nova 패키지 sys.path 주입
for _p in (str(Path.home() / "nova"), str(HERMES_HOME / "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# api_key 주입 — 1순위: .env HERMES_MASTER_APIKEY / 2순위: config.yaml model.api_key
def _inject_api_key() -> None:
    """API 키를 발견하면 관련 env var들을 채운다.

    P1 fix (2026-08-18): NOVA_LLM_PROVIDER/BASE_URL/MODEL을 원저자의 사설
    HMG 게이트웨이로 강제 주입하던 로직을 제거했다. base_url이 사용자
    환경(NOVA_LLM_BASE_URL 또는 nova.yaml)에 이미 설정된 경우에만 'hmg'
    provider를 선택하고, 그렇지 않으면 API 키 없이 동작하는 공개 'echo'
    provider로 안전하게 폴백한다 (nova/watcher/brain.py의
    _apply_master_key_llm_defaults()와 동일한 원칙).
    """
    try:
        import yaml as _yaml
        key = ""
        _env_path = HERMES_HOME / ".env"
        if _env_path.exists():
            for _ln in _env_path.read_text(errors="replace").splitlines():
                if _ln.strip().startswith("HERMES_MASTER_APIKEY="):
                    key = _ln.strip().split("=", 1)[1].strip()
                    break
        if not key:
            key = _yaml.safe_load((HERMES_HOME / "config.yaml").read_text()).get("model", {}).get("api_key", "")
        if key:
            has_base_url = bool(os.environ.get("NOVA_LLM_BASE_URL"))
            if not has_base_url:
                try:
                    _nova_yaml = NOVA_HOME / "nova.yaml"
                    if _nova_yaml.exists():
                        _raw = _yaml.safe_load(_nova_yaml.read_text()) or {}
                        has_base_url = bool((_raw.get("llm") or {}).get("base_url"))
                except Exception:
                    pass
            if has_base_url:
                os.environ.setdefault("NOVA_LLM_PROVIDER", "hmg")
                os.environ.setdefault("NOVA_LLM_MODEL", "claude-sonnet-4-6")
            else:
                os.environ.setdefault("NOVA_LLM_PROVIDER", "echo")
            for var in ("NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
                        "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY", "NOVA_CODEX_API_KEY",
                        "NOVA_IMAGE_GEN_API_KEY"):
                os.environ.setdefault(var, key)
    except Exception:
        pass

# NOTE (2026-08-18, Codex-audited round 5): this module-top-level call
# means merely IMPORTING this file (not just running it as a script)
# mutates process-wide os.environ as a side effect -- a design smell.
# In normal operation this is harmless today: the only real caller
# launches this file as a standalone `python nova_agent_worker.py`
# subprocess (see nova_orchestrator.py's WORKER_PY), never imports it as
# a library, so the side effect only ever applies to that dedicated
# process's own environment. It bit test isolation instead (importlib
# loading this module in a test fixture leaked NOVA_LLM_PROVIDER into
# the rest of the pytest process — fixed in
# tests/unit/test_agent_worker_sql_injection.py by snapshotting/
# restoring the affected env vars around the import). Left as-is rather
# than refactored into a lazy call, since doing so would risk changing
# runtime behavior of the one path this whole legacy nova/agents/ tree
# is designed around; flagging here for any future import-based reuse.
_inject_api_key()


# ── 로그 ─────────────────────────────────────────────────────────────────────
_AGENT = ""  # main()에서 설정

def _log(msg: str) -> None:
    ts  = datetime.now().strftime("%H:%M:%S")
    pid = os.getpid()
    line = f"[{ts}][pid={pid}][{_AGENT or 'worker'}] {msg}"
    print(line, flush=True)
    try:
        lf = NOVA_HOME / "logs" / f"agent_{_AGENT}.log"
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── brain.db 공유 접근 ────────────────────────────────────────────────────────
def _db(timeout: float = 15.0) -> sqlite3.Connection:
    c = sqlite3.connect(str(BRAIN_DB), timeout=timeout)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    return c


# ── KB + MEMORY 컨텍스트 읽기 (공유 읽기 전용) ────────────────────────────────
def read_shared_context(topic: str, agent: str = "") -> str:
    """
    nova_shared_kb 모듈 사용 — KB + wiki + MEMORY + 스프린트 진행 상황 통합.
    모든 에이전트가 동일한 지식베이스 기반으로 같은 방향으로 진행.
    """
    try:
        # nova_shared_kb.py를 직접 임포트 (같은 HERMES_HOME/bin 디렉토리)
        shared_kb_path = HERMES_HOME / "bin" / "nova_shared_kb.py"
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("nova_shared_kb", shared_kb_path)
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.read_context(topic, agent=agent, max_chars=3000)
    except Exception as e:
        # fallback: 기본 KB 읽기
        _log(f"nova_shared_kb 로드 실패, fallback 사용: {e}")
        return _read_context_fallback(topic)


def _read_context_fallback(topic: str) -> str:
    """nova_shared_kb 실패 시 기본 KB + MEMORY 읽기.

    SECURITY-015 (2026-08-18, deep audit round 5): the LIKE conditions
    used raw f-string interpolation of user-controlled keywords
    (`f"content LIKE '%{k}%'"`), building the WHOLE WHERE clause as a
    single f-string. `topic` originates from `context.get("topic", ...)`
    -- i.e. CLI `--context topic=...`, the same attacker-controlled input
    class that caused SECURITY-003 (shell injection). Reproduced: a
    completely ordinary English word containing an apostrophe (e.g.
    "don't") crashes the query with a syntax error. Note (Codex-reviewed
    round 5): the crash is silently swallowed by the surrounding
    `except Exception: pass` here, so the practical impact is a silent
    KB-context lookup failure (degraded results), not a process crash or
    denial of service -- but it's still a real bug: completely ordinary
    user text should never break a query. Fixed by using parameterized
    placeholders (`?`) instead of string-formatting the keyword values
    into the SQL text; the LIKE wildcards ('%') are still applied but as
    part of the bound parameter value, never as SQL syntax.
    """
    parts = []
    try:
        uri  = f"file:{BRAIN_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        kws  = [w.lower() for w in topic.split() if len(w) > 2][:4]
        if kws:
            cond = " OR ".join("content LIKE ?" for _ in kws)
            params = [f"%{k}%" for k in kws]
            rows = conn.execute(
                f"SELECT section, content FROM page_chunks WHERE {cond} LIMIT 5",
                params,
            ).fetchall()
            if rows:
                parts.append("=== KB ===\n" + "\n".join(
                    f"[{s}] {(c or '')[:200]}" for s, c in rows))
        conn.close()
    except Exception:
        pass
    if MEMORY_FILE.exists():
        try:
            parts.append("=== MEMORY ===\n" +
                         MEMORY_FILE.read_text(encoding="utf-8", errors="ignore")[:500])
        except Exception:
            pass
    return "\n\n".join(parts)


# ── brain.db 쓰기 (WAL 직렬화) ────────────────────────────────────────────────
def record_activity(agent: str, task_id: str, action: str,
                    result: str, duration_s: float, summary: str) -> None:
    try:
        conn = _db()
        conn.execute(
            "INSERT OR IGNORE INTO agent_activity "
            "(id, agent, task_id, action, result, duration_s, summary, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], agent, task_id, action,
             result, duration_s, summary[:500],
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _log(f"activity 기록 실패 (무시): {e}")


def broadcast(agent: str, status: str, detail: str = "") -> None:
    """brain.db hermes_events → 다른 에이전트가 상태 공유"""
    try:
        conn = _db()
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO hermes_events "
            "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, f"AGENT_STATUS:{status}", "info",
             f"[{agent}] {status}", detail[:400], agent, 0, now)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def write_handoff(agent: str, harness: str, context: dict,
                  summary: str, ok: bool) -> None:
    """완료 결과 → workspace/handoff.json (다음 에이전트가 읽음). atomic rename."""
    ws = NOVA_HOME / "workspace" / harness
    ws.mkdir(parents=True, exist_ok=True)
    report = ws / "report.md"
    payload = json.dumps({
        "from_agent":     agent,
        "harness":        harness,
        "context":        context,
        "output_summary": summary[:800],
        "ok":             ok,
        "completed_at":   datetime.now(timezone.utc).isoformat(),
        "report_path":    str(report) if report.exists() else None,
        "pid":            os.getpid(),
    }, ensure_ascii=False, indent=2)
    # atomic rename으로 부분 기록 방지
    tmp = ws / "handoff.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(ws / "handoff.json")


# ── KB 동기화 ─────────────────────────────────────────────────────────────────
def sync_kb() -> None:
    """harness 완료 후 workspace → brain.db 인덱싱.
    주의: nova_brain.py가 sqlite_vec를 요구하므로 /usr/bin/python3 사용
    (Hermes venv python에는 sqlite_vec 없음)
    """
    kb_sync = HERMES_HOME / "bin" / "nova_kb_sync.py"
    if not kb_sync.exists():
        return
    # sqlite_vec가 설치된 python3 우선 사용
    for py in ("/usr/bin/python3", "/usr/local/bin/python3", sys.executable):
        if not Path(py).exists():
            continue
        r = subprocess.run(
            [py, str(kb_sync)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 "HERMES_HOME": str(HERMES_HOME), "NOVA_HOME": str(NOVA_HOME),
                 "PYTHONPATH": str(HERMES_HOME / "bin") + ":" + str(Path.home() / "nova")}
        )
        if r.returncode == 0:
            _log("KB 동기화 완료")
            return
        if "sqlite_vec" not in r.stderr:
            _log(f"kb_sync 실패: {r.stderr[:80]}")
            return
        # sqlite_vec 없으면 다음 python 시도
    _log("kb_sync: sqlite_vec 지원 python3 없음 — 스킵")


# ── kanban 직접 처리 (오케스트레이터 불필요) ──────────────────────────────────

def _on_done_register_takes(board: str, task_id: str, harness_name: str, summary: str) -> None:
    """harness 완료 후 brain.db takes 자동 등록 (insight/pattern/lesson 분류)."""
    import sqlite3, uuid, re
    from datetime import datetime, timezone
    BRAIN = NOVA_HOME / "brain.db"
    if not BRAIN.exists(): return
    ws_dir = NOVA_HOME / "workspace" / harness_name
    report = ws_dir / "report.md"
    if not report.exists(): return
    text = report.read_text(encoding="utf-8", errors="ignore")
    if len(text) < 50: return
    path_key = "workspace/" + harness_name + "/report.md"
    now = datetime.now(timezone.utc).isoformat()
    # 섹션별 kind 판별
    SECTION_KIND = {
        'root cause': 'insight', 'rca': 'insight', '원인': 'insight',
        'lesson': 'lesson',      '교훈': 'lesson',
        'pattern': 'pattern',    '패턴': 'pattern', '반복': 'pattern',
        'finding': 'insight',    '발견': 'insight', '결론': 'insight',
        'recommendation': 'insight', '권고': 'insight',
    }
    takes_to_insert = []
    lines = text.split(chr(10))
    cur_kind = 'fact'
    cur_lines = []
    for line in lines:
        m = re.match(r'^#+\s+(.*)', line)
        if m:
            if cur_lines:
                takes_to_insert.append((cur_kind, ' '.join(cur_lines[:3])))
            heading = m.group(1).lower()
            cur_kind = next((v for k, v in SECTION_KIND.items() if k in heading), 'fact')
            cur_lines = []
        else:
            stripped = line.strip().lstrip('-').strip()
            if stripped and len(stripped) > 10:
                cur_lines.append(stripped[:120])
    if cur_lines:
        takes_to_insert.append((cur_kind, ' '.join(cur_lines[:3])))
    if not takes_to_insert:
        takes_to_insert = [('fact', text.strip()[:200].replace(chr(10), ' '))]
    with sqlite3.connect(str(BRAIN)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        row = c.execute("SELECT id FROM pages WHERE path=?", (path_key,)).fetchone()
        if not row:
            page_id = uuid.uuid4().hex
            first_line = (text.lstrip('#').split(chr(10))[0].strip() or harness_name)[:80]
            c.execute(
                "INSERT OR IGNORE INTO pages (id,path,title,page_type,compiled_truth,char_count,indexed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (page_id, path_key, first_line, 'workspace', text[:3000], len(text), now, now, now)
            )
        else:
            page_id = row[0]
        existing = c.execute("SELECT COUNT(*) FROM takes WHERE page_id=?", (page_id,)).fetchone()[0]
        if existing > 0: return
        cnt = 0
        for kind, claim in takes_to_insert[:5]:
            if not claim.strip(): continue
            weight = {'insight':0.8,'lesson':0.85,'pattern':0.9,'fact':0.7}.get(kind, 0.7)
            c.execute(
                "INSERT OR IGNORE INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], page_id, kind, 'nova-harness', claim[:200], weight, now, now)
            )
            cnt += 1
        c.commit()
        _log("takes 등록: " + harness_name + " kind_mix=" + str(cnt))


def kanban_complete(board: str, task_id: str, harness: str = "") -> None:
    """kanban complete 처리. harness report.md 내용을 --result로 전달해 DoD 게이트 통과."""
    if not board or not task_id:
        return
    # harness workspace의 report.md를 읽어 DoD 키워드가 들어있는 결과를 task.result에 저장
    result_text = ""
    if harness:
        for fname in ("report.md", "summary_report.md", "dod_summary.md"):
            rpt = NOVA_HOME / "workspace" / harness / fname
            if rpt.exists():
                try:
                    result_text = rpt.read_text(encoding="utf-8", errors="ignore")[:2000]
                    break
                except Exception:
                    pass
    cmd = ["hermes", "kanban", "--board", board, "complete", task_id]
    if result_text:
        cmd += ["--result", result_text[:800]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        _log(f"kanban complete: {task_id} (result={'있음' if result_text else '없음'})")
    else:
        _log(f"kanban complete 실패: {r.stderr[:60]}")


def kanban_block(board: str, task_id: str, reason: str) -> None:
    if not board or not task_id:
        return
    subprocess.run(
        ["hermes", "kanban", "--board", board, "block", task_id, reason[:100]],
        capture_output=True, text=True, timeout=10
    )
    _log(f"kanban block: {task_id}")


# ── harness 실행 ─────────────────────────────────────────────────────────────
def run_harness(agent: str, harness_name: str, context: dict) -> bool:
    """HarnessLoader + Orchestrator 직접 호출 (독립 프로세스 내에서)"""
    from nova.core.config import load_config        # type: ignore
    from nova.core.harness import HarnessLoader     # type: ignore
    from nova.core.orchestrator import Orchestrator  # type: ignore

    cfg = load_config(str(NOVA_HOME / "nova.yaml"))
    cfg.harnesses_dir = str(Path(cfg.harnesses_dir).expanduser())
    cfg.workspace     = str(Path(cfg.workspace).expanduser())

    loader  = HarnessLoader(cfg.harnesses_dir)
    harness = loader.load(harness_name)
    orch    = Orchestrator(cfg)

    # KB + MEMORY 컨텍스트를 harness context에 주입
    shared_ctx = read_shared_context(context.get("topic", agent))
    if shared_ctx:
        context = {**context, "kb_context": shared_ctx[:2000]}

    return orch.run(harness, context=context, resume=False)


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    global _AGENT
    parser = argparse.ArgumentParser(description="NOVA 독립 에이전트 워커")
    parser.add_argument("--agent",    required=True)
    parser.add_argument("--harness",  required=True)
    parser.add_argument("--task-id",  required=True, dest="task_id")
    parser.add_argument("--board",    default="nova-loop")
    parser.add_argument("--context",  default="{}")
    args = parser.parse_args()

    _AGENT   = args.agent
    agent    = args.agent
    harness  = args.harness
    task_id  = args.task_id
    board    = args.board
    context  = json.loads(args.context)

    # SIGTERM: 정리 후 종료
    def _on_term(sig, frame):
        broadcast(agent, "KILLED", "SIGTERM received")
        kanban_block(board, task_id, "에이전트 강제 종료 (SIGTERM)")
        sys.exit(130)
    signal.signal(signal.SIGTERM, _on_term)

    _log(f"시작 — harness={harness} task={task_id} board={board}")
    broadcast(agent, "RUNNING", json.dumps(context, ensure_ascii=False)[:200])

    t0 = time.time()
    ok = False
    summary = ""

    try:
        ok      = run_harness(agent, harness, context)
        summary = f"harness={harness} ok={ok} pid={os.getpid()}"
        _log(f"{'완료 OK' if ok else '완료 FAIL'}")

        if ok:
            sync_kb()
            write_handoff(agent, harness, context, summary, ok)
        else:
            # WARN-2 수정: 실패 시에도 handoff 기록 (다음 에이전트가 실패 컨텍스트 인식)
            write_handoff(agent, harness, context, f"FAILED: {summary}", ok)

    except Exception as e:
        summary = f"예외: {traceback.format_exc()[-500:]}"
        _log(f"예외: {e}")
        # WARN-5 수정: exception 시에도 handoff 기록 (다음 에이전트 handoff chain 유지)
        try:
            write_handoff(agent, harness, context, f"EXCEPTION: {summary[:200]}", False)
        except Exception:
            pass

    duration = round(time.time() - t0, 2)
    status   = "DONE" if ok else "FAILED"

    broadcast(agent, status, summary[:300])
    record_activity(agent, task_id, harness, status, duration, summary[:300])

    # ★ kanban 직접 처리 (오케스트레이터 생존 여부와 무관하게 확실히 처리)
    if ok:
        kanban_complete(board, task_id, harness)
        # harness 완료 → takes 자동 등록
        try:
            _on_done_register_takes(board, task_id, harness, summary)
        except Exception as _te:
            _log(f'on_done_takes skip: {_te}')
    else:
        kanban_block(board, task_id, f"{agent} harness 실패: {summary[:60]}")

    # ★ 스프린트 진행 상황 공유 기록 (다음 에이전트가 read_context로 읽음)
    try:
        shared_kb_path = HERMES_HOME / "bin" / "nova_shared_kb.py"
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("nova_shared_kb", shared_kb_path)
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.write_progress(agent, "DONE" if ok else "FAILED", summary[:200], ok)
    except Exception:
        pass

    # 오케스트레이터가 읽는 결과 JSON
    result_json = json.dumps({
        "agent": agent, "harness": harness,
        "task_id": task_id, "board": board,
        "ok": ok, "duration": duration, "summary": summary[:200],
        "pid": os.getpid(),
    }, ensure_ascii=False)
    print(f"__AGENT_RESULT__{result_json}__END_RESULT__", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
