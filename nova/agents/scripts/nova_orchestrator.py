#!/usr/bin/env python3
"""
nova_orchestrator.py — NOVA 멀티에이전트 오케스트레이터 v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

설계 원칙:
  - 에이전트별 subprocess.Popen() → 독립 PID
  - 워커(nova_agent_worker.py)가 kanban complete/block 자체 처리
    (오케스트레이터 생존 여부와 무관)
  - 오케스트레이터는 파견 + 감시 + 로그만 담당
  - PARALLEL_GROUPS: canary+health, retro+document 동시 Popen

공유 자원:
  brain.db  WAL 동시 읽기 OK
  KB 파일   읽기 전용
  MEMORY.md 읽기 전용
  handoff.json workspace 읽기/쓰기

실행:
  # kanban ready 태스크 전체 파견 (chain_engine이 호출)
  python3 nova_orchestrator.py --dispatch --board nova-loop

  # 단일 에이전트 파견 후 완료 대기
  python3 nova_orchestrator.py --agent nova-dev \\
      --task-id t_xxx --board nova-loop \\
      --context '{"topic":"..."}' --wait

  # 실행 중 에이전트 현황
  python3 nova_orchestrator.py --status
"""
from __future__ import annotations

import os, sys, json, uuid, time, sqlite3, subprocess, threading, signal, argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 환경 ─────────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
BRAIN_DB    = NOVA_HOME / "brain.db"
WORKER_PY   = HERMES_HOME / "bin" / "nova_agent_worker.py"

# api_key 주입 — 1순위: .env HERMES_MASTER_APIKEY / 2순위: config.yaml model.api_key
def _inject_api_key() -> None:
    try:
        import yaml as _y
        key = ""
        _env_path = HERMES_HOME / ".env"
        if _env_path.exists():
            for _ln in _env_path.read_text(errors="replace").splitlines():
                if _ln.strip().startswith("HERMES_MASTER_APIKEY="):
                    key = _ln.strip().split("=", 1)[1].strip()
                    break
        if not key:
            key = _y.safe_load((HERMES_HOME / "config.yaml").read_text()).get("model", {}).get("api_key", "")
        if key:
            for v in ("NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
                      "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY", "NOVA_CODEX_API_KEY",
                      "NOVA_IMAGE_GEN_API_KEY"):
                os.environ.setdefault(v, key)
    except Exception:
        pass

_inject_api_key()

# ── HARNESS_AGENTS 매핑 ───────────────────────────────────────────────────────
HARNESS_AGENTS: dict[str, str] = {
    "nova-autoplan":         "research",
    "nova-dev":              "code_implement",
    "nova-review":           "code_review",
    "nova-cso":              "security_sign_off",
    "nova-qa":               "qa",
    "nova-ship":             "ship",
    "nova-checkpoint":       "go_nogo",
    "nova-canary":           "canary",
    "nova-health":           "health",
    "nova-evaluator":        "kpi_evaluate",
    "nova-retro":            "retro",
    "nova-learn":            "learn",
    "nova-document":         "document_gen",
    "nova-document-release": "document_release",
    "nova-investigate":      "investigate",
    "nova-research":         "research",
    "nova-sysaudit":         "system_audit",    # BUG-FAIL-1: 누락 추가 — 체인 최후단계 파견 가능
    "nova-marketing":        "go_nogo",
    "nova-strategy":         "document_gen",
    "nova-careful":          "security_sign_off",
    "nova-validator":        "qa",
    "nova-benchmark":        "kpi_evaluate",
}

# 병렬 파견 그룹 (FORK 후 동시 ready 쌍)
PARALLEL_GROUPS: list[frozenset] = [
    frozenset({"nova-canary", "nova-health"}),    # checkpoint FORK 병렬
    frozenset({"nova-retro",  "nova-learn"}),     # evaluator FORK 병렬 (BUG-PARALLEL 수정: document→learn)
]

AGENT_TIMEOUT = 600  # 워커 프로세스 최대 실행 시간

import threading, signal

# ── 로그 ─────────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()

def log(msg: str, src: str = "orchestrator") -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][{src}] {msg}"
    with _log_lock:
        print(line, flush=True)
    try:
        lf = NOVA_HOME / "logs" / "orchestrator.log"
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 에이전트 레지스트리 ───────────────────────────────────────────────────────
class AgentRegistry:
    """실행 중 에이전트 PID 추적 (thread-safe)"""
    def __init__(self):
        self._lock = threading.Lock()
        self._procs: dict[str, dict] = {}

    def add(self, agent: str, proc: subprocess.Popen, task_id: str) -> None:
        with self._lock:
            self._procs[agent] = {"proc": proc, "task_id": task_id,
                                   "pid": proc.pid, "started": time.time()}
        log(f"등록: {agent} pid={proc.pid}")

    def remove(self, agent: str) -> None:
        with self._lock:
            self._procs.pop(agent, None)

    def running(self) -> list[str]:
        with self._lock:
            return list(self._procs.keys())

    def kill_all(self) -> None:
        with self._lock:
            for ag, info in list(self._procs.items()):
                try:
                    info["proc"].terminate()
                    log(f"종료 요청: {ag} pid={info['pid']}")
                except Exception:
                    pass
            self._procs.clear()

    def status(self) -> list[dict]:
        with self._lock:
            now = time.time()
            return [
                {"agent": ag, "pid": v["pid"], "task_id": v["task_id"],
                 "elapsed_s": round(now - v["started"], 1)}
                for ag, v in self._procs.items()
            ]


REGISTRY = AgentRegistry()


# ── 에이전트 파견 ─────────────────────────────────────────────────────────────
def _worker_env() -> dict:
    return {
        **os.environ,
        "HERMES_HOME": str(HERMES_HOME),
        "NOVA_HOME":   str(NOVA_HOME),
        "PYTHONPATH":  str(HERMES_HOME / "bin") + ":" + str(Path.home() / "nova"),
    }


def dispatch_agent(agent: str, task_id: str, board: str,
                   context: dict, on_done=None) -> Optional[subprocess.Popen]:
    """
    에이전트를 독립 subprocess로 파견.
    워커 자체가 kanban complete/block 처리 → 오케스트레이터 생존 불필요.
    on_done(agent, task_id, ok, summary) 는 선택적 감시 콜백.
    """
    harness = HARNESS_AGENTS.get(agent)
    if not harness:
        log(f"harness 없음: {agent} passthrough", agent)
        return None

    if agent in REGISTRY.running():
        log(f"이미 실행 중: {agent}", agent)
        return None

    cmd = [
        sys.executable, str(WORKER_PY),
        "--agent",   agent,
        "--harness", harness,
        "--task-id", task_id,
        "--board",   board,
        "--context", json.dumps(context, ensure_ascii=False),
    ]

    log(f"파견: {agent}→{harness} pid=? task={task_id}")
    try:
        proc = subprocess.Popen(
            cmd, env=_worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        log(f"Popen 실패: {agent} {e}")
        return None

    REGISTRY.add(agent, proc, task_id)

    # 감시 스레드 (비데몬 — 오케스트레이터 종료 후에도 완료까지 유지)
    t = threading.Thread(
        target=_watch, args=(agent, task_id, proc, on_done),
        daemon=False,  # ★ 비데몬: 프로세스 종료 후에도 완료 보장
        name=f"watch-{agent}",
    )
    t.start()
    log(f"파견 완료: {agent} pid={proc.pid}")
    return proc


def _watch(agent: str, task_id: str, proc: subprocess.Popen,
           on_done=None) -> None:
    """워커 프로세스 완료 감시 (비데몬 스레드)"""
    try:
        stdout, stderr = proc.communicate(timeout=AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT ({AGENT_TIMEOUT}s): {agent}", agent)
        proc.kill()
        # BUG-D4 수정: kill 후 communicate에 timeout 추가 — 고아 자식이 pipe 보유 시 hang 방지
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "communicate timeout after kill"
        REGISTRY.remove(agent)
        if on_done:
            on_done(agent, task_id, False, "TIMEOUT")
        return

    REGISTRY.remove(agent)
    ok  = (proc.returncode == 0)

    # 결과 JSON 파싱
    result = {}
    if stdout and "__AGENT_RESULT__" in stdout:
        try:
            raw    = stdout.split("__AGENT_RESULT__")[1].split("__END_RESULT__")[0]
            result = json.loads(raw)
        except Exception:
            pass

    summary = result.get("summary", (stderr or "")[:200])
    log(f"완료: {agent} ok={ok} pid={proc.pid} {summary[:50]}", agent)

    if on_done:
        on_done(agent, task_id, ok, summary)


# ── kanban 연동 ───────────────────────────────────────────────────────────────
def kanban_list(board: str) -> list[dict]:
    r = subprocess.run(
        ["hermes", "kanban", "--board", board, "list", "--json"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            return json.loads(r.stdout)
        except Exception:
            pass
    return []


# ── ready 태스크 일괄 파견 ────────────────────────────────────────────────────
def dispatch_ready(board: str, wait: bool = False) -> int:
    """
    kanban ready 태스크 → 에이전트별 독립 Popen 파견.
    PARALLEL_GROUPS 에 속한 에이전트는 동시 파견.
    워커 자체가 kanban complete/block 처리하므로 오케스트레이터는 감시만.
    """
    tasks  = kanban_list(board)
    ready  = [t for t in tasks if t.get("status") == "ready"
              and t.get("assignee") in HARNESS_AGENTS]

    if not ready:
        log(f"ready 태스크 없음 (board={board})")
        return 0

    log(f"ready 태스크 {len(ready)}개 발견: {[t.get('assignee') for t in ready]}")

    dispatched  = 0
    handled     = set()
    ready_set   = {t.get("assignee", "") for t in ready}

    # 1) 병렬 그룹 동시 파견
    for grp in PARALLEL_GROUPS:
        overlap = grp & ready_set
        if len(overlap) < 2:
            continue
        log(f"병렬 파견: {list(overlap)}")
        for task in [t for t in ready if t.get("assignee") in overlap]:
            ag  = task["assignee"]
            tid = task["id"]
            ttl = task.get("title", "")
            proc = dispatch_agent(ag, tid, board, {"topic": ttl[:80]})
            if proc:
                dispatched += 1
                handled.add(ag)

    # 2) 단독 순차 파견
    for task in ready:
        ag  = task.get("assignee", "")
        tid = task.get("id", "")
        ttl = task.get("title", "")
        if ag in handled:
            continue
        proc = dispatch_agent(ag, tid, board, {"topic": ttl[:80]})
        if proc:
            dispatched += 1

    if wait:
        log("완료 대기...")
        while REGISTRY.running():
            time.sleep(2)
        log("전체 에이전트 완료")

    return dispatched


# ── 진입점 ───────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="NOVA 멀티에이전트 오케스트레이터")
    parser.add_argument("--board",    default="nova-loop")
    parser.add_argument("--agent",    help="단일 에이전트")
    parser.add_argument("--harness",  help="단일 파견 harness (선택)")
    parser.add_argument("--task-id",  dest="task_id",
                        default="manual-" + uuid.uuid4().hex[:8])
    parser.add_argument("--context",  default="{}")
    parser.add_argument("--dispatch", action="store_true", help="ready 일괄 파견")
    parser.add_argument("--status",   action="store_true", help="현황")
    parser.add_argument("--wait",     action="store_true", help="완료까지 대기")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda s, f: (REGISTRY.kill_all(), sys.exit(0)))

    if args.status:
        st = REGISTRY.status()
        print(json.dumps({"running": st, "count": len(st)},
                         indent=2, ensure_ascii=False))
        return

    if args.agent:
        harness = args.harness or HARNESS_AGENTS.get(args.agent, "")
        if not harness:
            print(f"harness 없음: {args.agent}", file=sys.stderr)
            sys.exit(1)
        ctx = json.loads(args.context)
        done_ev   = threading.Event()
        result_hd = {}

        def _done(ag, tid, ok, summary):
            result_hd.update({"ok": ok, "summary": summary})
            done_ev.set()

        proc = dispatch_agent(args.agent, args.task_id, args.board, ctx, on_done=_done)
        if not proc:
            sys.exit(1)

        if args.wait:
            done_ev.wait(timeout=AGENT_TIMEOUT + 30)
            print(json.dumps(result_hd, ensure_ascii=False))
            sys.exit(0 if result_hd.get("ok") else 1)
        else:
            print(json.dumps({"agent": args.agent, "pid": proc.pid,
                               "task_id": args.task_id}, ensure_ascii=False))
            # 비데몬 스레드가 살아있으므로 프로세스는 완료까지 대기
            for t in threading.enumerate():
                if t.name.startswith("watch-") and t.is_alive():
                    t.join()

    elif args.dispatch:
        n = dispatch_ready(args.board, wait=args.wait)
        log(f"파견 완료: {n}개")
        print(json.dumps({"dispatched": n, "running": REGISTRY.running()},
                         ensure_ascii=False))
        if not args.wait:
            # 비데몬 감시 스레드 완료까지 대기
            for t in threading.enumerate():
                if t.name.startswith("watch-") and t.is_alive():
                    t.join()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
