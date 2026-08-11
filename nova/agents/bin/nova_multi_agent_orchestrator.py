#!/usr/bin/env python3
"""
nova_multi_agent_orchestrator.py — NOVA 실질 멀티에이전트 오케스트레이터
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

구조:
  Orchestrator (이 파일) — 에이전트 파견/감시/결과 수집
    ├─ AgentWorker × N (독립 subprocess, 각자 harness 실행)
    │     공유 채널: brain.db의 agent_activity 테이블
    │     공유 KB: ~/.hermes/kb/ (read-only during run, write on complete)
    └─ KBBus — agent_activity 기반 에이전트 간 상태 공유

멀티에이전트 실행 모델:
  - 독립 병렬 실행 가능 에이전트 쌍 (PARALLEL_SAFE):
      (nova-canary, nova-health)       ← nova-ship 이후 동시
      (nova-retro, nova-document)      ← nova-evaluator 이후 동시
  - 직렬 체인 (STAGE_ORDER 순서 준수)
  - 에이전트 간 통신: brain.db hermes_events (AGENT_MSG type)

사용:
  python3 nova_multi_agent_orchestrator.py --agent nova-dev --topic "구현 주제"
  python3 nova_multi_agent_orchestrator.py --board nova-loop --dispatch-ready
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 환경 설정 ─────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))
BRAIN_DB    = NOVA_HOME / "brain.db"
KB_ROOT     = HERMES_HOME / "kb"
CHAIN_ENGINE = HERMES_HOME / "scripts" / "nova_chain_engine.py"

# ── 병렬 실행 안전 에이전트 쌍 (의존관계 없는 독립 쌍) ────────────────────────
# 형식: {트리거 에이전트(완료 시): [동시 실행 가능한 에이전트 목록]}
PARALLEL_SAFE: dict[str, list[str]] = {
    "nova-ship":      ["nova-canary", "nova-health"],    # 배포 후 모니터링 병렬
    "nova-evaluator": ["nova-retro", "nova-document"],   # 평가 후 학습+문서 병렬
    "nova-autoplan":  ["nova-research"],                 # 기획 시 리서치 병렬
}

# ── 에이전트 → harness 매핑 (chain_engine.py와 동기화 필수) ───────────────────
HARNESS_MAP: dict[str, str] = {
    "nova-research":          "research",
    "nova-autoplan":          "research",
    "nova-dev":               "code_implement",
    "nova-review":            "code_review",
    "nova-qa":                "qa",
    "nova-checkpoint":        "go_nogo",
    "nova-cso":               "security_sign_off",
    "nova-evaluator":         "kpi_evaluate",
    "nova-ship":              "ship",
    "nova-canary":            "canary",
    "nova-health":            "health",
    "nova-retro":             "retro",
    "nova-learn":             "learn",
    "nova-document":          "document_gen",
    "nova-document-release":  "document_release",
    "nova-investigate":       "investigate",
}

# ── 로깅 ─────────────────────────────────────────────────────────────────────
LOG_FILE = NOVA_HOME / "logs" / "multi_agent_orchestrator.log"
_log_lock = threading.Lock()

def log(msg: str, agent: str = "orchestrator") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{agent}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── brain.db 에이전트 통신 버스 ───────────────────────────────────────────────
class KBBus:
    """brain.db hermes_events를 에이전트 간 메시지 채널로 사용"""

    def __init__(self, db_path: Path = BRAIN_DB):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def send(self, from_agent: str, to_agent: str, msg_type: str, payload: dict) -> None:
        """에이전트 간 메시지 전송"""
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        detail = json.dumps({"from": from_agent, "to": to_agent, "payload": payload}, ensure_ascii=False)
        try:
            conn = self._conn()
            conn.execute(
                "INSERT OR IGNORE INTO hermes_events "
                "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eid, f"AGENT_MSG:{msg_type}", "info",
                 f"[{from_agent}→{to_agent}] {msg_type}",
                 detail, from_agent, 0, now)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log(f"KBBus.send 실패: {e}")

    def read(self, to_agent: str, msg_type: str = "", mark_read: bool = True) -> list[dict]:
        """수신 메시지 읽기"""
        try:
            conn = self._conn()
            pattern = f"AGENT_MSG:{msg_type}%" if msg_type else "AGENT_MSG:%"
            rows = conn.execute(
                "SELECT id, event_type, detail FROM hermes_events "
                "WHERE event_type LIKE ? AND is_read=0",
                (pattern,)
            ).fetchall()
            msgs = []
            for eid, etype, detail in rows:
                try:
                    d = json.loads(detail)
                    if d.get("to") == to_agent or to_agent == "*":
                        msgs.append({"id": eid, "type": etype, "data": d})
                        if mark_read:
                            conn.execute("UPDATE hermes_events SET is_read=1 WHERE id=?", (eid,))
                except Exception:
                    pass
            if mark_read:
                conn.commit()
            conn.close()
            return msgs
        except Exception as e:
            log(f"KBBus.read 실패: {e}")
            return []

    def broadcast_status(self, agent: str, status: str, context: dict) -> None:
        """에이전트 상태 브로드캐스트 (모든 에이전트가 진행 상황 파악)"""
        self.send(agent, "*", "STATUS_UPDATE", {"status": status, "context": context})


# ── 에이전트 워커 ─────────────────────────────────────────────────────────────
class AgentWorker:
    """단일 에이전트 실행 단위 — 독립 subprocess로 harness 실행"""

    def __init__(self, agent: str, context: dict, kb_bus: KBBus):
        self.agent   = agent
        self.context = context
        self.kb_bus  = kb_bus
        self.harness = HARNESS_MAP.get(agent, "")
        self.result  = {"ok": False, "agent": agent, "output": ""}

    def run(self) -> dict:
        if not self.harness:
            log(f"harness 없음 — passthrough", agent=self.agent)
            self.result = {"ok": True, "agent": self.agent, "output": "passthrough"}
            return self.result

        # 1. 시작 브로드캐스트
        self.kb_bus.broadcast_status(self.agent, "RUNNING", self.context)
        log(f"harness={self.harness} 시작", agent=self.agent)

        # 이전 에이전트 handoff 읽기 — KB 공유 컨텍스트 주입
        handoff_context = self._read_handoff_context()
        if handoff_context:
            self.context = {**self.context, **handoff_context}
            log(f"이전 에이전트 handoff 수신: {list(handoff_context.keys())}", agent=self.agent)

        try:
            # chain_engine._execute_harness_for_agent 와 동일한 실행 방식:
            # HarnessLoader + Orchestrator 직접 호출 (nova.cli subprocess 불필요)
            import sys as _sys
            nova_src   = Path.home() / "nova"
            hermes_bin = HERMES_HOME / "bin"
            for p in (str(hermes_bin), str(nova_src)):
                if p not in _sys.path:
                    _sys.path.insert(0, p)

            from nova.core.config import load_config       # type: ignore
            from nova.core.harness import HarnessLoader    # type: ignore
            from nova.core.orchestrator import Orchestrator # type: ignore

            cfg = load_config(str(NOVA_HOME / "nova.yaml"))
            cfg.harnesses_dir = str(Path(cfg.harnesses_dir).expanduser())
            cfg.workspace     = str(Path(cfg.workspace).expanduser())

            # api_key 주입 — 1순위: .env HERMES_MASTER_APIKEY / 2순위: config.yaml model.api_key
            try:
                import yaml as _yaml
                api_key = ""
                _env_path = HERMES_HOME / ".env"
                if _env_path.exists():
                    for _ln in _env_path.read_text(errors="replace").splitlines():
                        if _ln.strip().startswith("HERMES_MASTER_APIKEY="):
                            api_key = _ln.strip().split("=", 1)[1].strip()
                            break
                if not api_key:
                    api_key = _yaml.safe_load((HERMES_HOME / "config.yaml").read_text()).get("model", {}).get("api_key", "")
                if api_key:
                    for var in ("NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
                                "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY", "NOVA_CODEX_API_KEY",
                                "NOVA_IMAGE_GEN_API_KEY"):
                        os.environ.setdefault(var, api_key)
                    log(f"api_key 주입 완료 (len={len(api_key)})", agent=self.agent)
            except Exception as e:
                log(f"api_key 주입 실패 (무시): {e}", agent=self.agent)

            loader  = HarnessLoader(cfg.harnesses_dir)
            harness = loader.load(self.harness)
            orch    = Orchestrator(cfg)
            ok      = orch.run(harness, context=self.context, resume=False)

            if ok:
                self._sync_kb_after_run()
                self.kb_bus.broadcast_status(self.agent, "DONE", {
                    **self.context, "harness": self.harness
                })
                self._write_handoff(f"harness={self.harness} OK")
            else:
                self.kb_bus.broadcast_status(self.agent, "FAILED", self.context)

            log(f"{'OK' if ok else 'FAIL'} harness={self.harness}", agent=self.agent)
            self.result = {"ok": ok, "agent": self.agent, "output": self.harness}

        except Exception as e:
            log(f"예외: {e}", agent=self.agent)
            self.kb_bus.broadcast_status(self.agent, "FAILED", {**self.context, "error": str(e)})
            self.result = {"ok": False, "agent": self.agent, "output": str(e)}

        return self.result

    def _sync_kb_after_run(self) -> None:
        """harness 완료 후 workspace → brain.db KB 동기화"""
        kb_sync = HERMES_HOME / "bin" / "nova_kb_sync.py"
        if not kb_sync.exists():
            return
        try:
            r = subprocess.run(
                [sys.executable, str(kb_sync)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "NOVA_HOME": str(NOVA_HOME)}
            )
            if r.returncode == 0:
                log("KB 동기화 완료", agent=self.agent)
            else:
                log(f"KB 동기화 실패: {r.stderr[:100]}", agent=self.agent)
        except Exception as e:
            log(f"KB 동기화 예외: {e}", agent=self.agent)

    def _write_handoff(self, output: str) -> None:
        """다음 에이전트를 위한 핸드오프 파일 생성 (atomic rename으로 경쟁 방지)"""
        workspace = NOVA_HOME / "workspace" / self.harness
        handoff = workspace / "handoff.json"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            handoff_data = {
                "from_agent": self.agent,
                "harness": self.harness,
                "context": self.context,
                "output_summary": output[:500],
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "report_path": str(workspace / "report.md") if (workspace / "report.md").exists() else None,
            }
            tmp = handoff.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(handoff_data, ensure_ascii=False, indent=2))
            tmp.replace(handoff)  # atomic
        except Exception as e:
            log(f"handoff 저장 실패: {e}", agent=self.agent)

    def _read_handoff_context(self) -> dict:
        """
        KB에서 이전 에이전트들의 handoff.json 읽기.
        모든 harness workspace를 순회해 최신 handoff 정보 수집.
        에이전트 간 공유 진행 상황 파악에 사용.
        """
        context_update = {}
        workspace_root = NOVA_HOME / "workspace"
        if not workspace_root.exists():
            return {}
        try:
            handoffs = []
            for handoff_file in workspace_root.rglob("handoff.json"):
                try:
                    data = json.loads(handoff_file.read_text())
                    handoffs.append(data)
                except Exception:
                    pass
            if not handoffs:
                return {}
            # 최신 순 정렬
            handoffs.sort(key=lambda x: x.get("completed_at", ""), reverse=True)
            # 최근 3개 에이전트의 output_summary를 prior_context로 주입
            prior = []
            for h in handoffs[:3]:
                agent = h.get("from_agent", "")
                summary = h.get("output_summary", "")
                report_path = h.get("report_path")
                if summary:
                    prior.append(f"[{agent}] {summary[:200]}")
                # report.md가 있으면 내용도 읽기
                if report_path and Path(report_path).exists():
                    try:
                        report_text = Path(report_path).read_text(encoding="utf-8", errors="ignore")
                        prior.append(f"[{agent} report] {report_text[:400]}")
                    except Exception:
                        pass
            if prior:
                context_update["prior_agent_context"] = "\n\n".join(prior)
                context_update["prior_agents"] = [h.get("from_agent") for h in handoffs[:3]]
        except Exception as e:
            log(f"handoff 읽기 실패 (무시): {e}", agent=self.agent)
        return context_update


# ── 멀티에이전트 오케스트레이터 ──────────────────────────────────────────────
class MultiAgentOrchestrator:
    """
    멀티에이전트 실행 조율자.
    
    기능:
      1. kanban ready 태스크 수집
      2. 병렬 실행 안전 에이전트 쌍 → ThreadPoolExecutor 병렬 실행
      3. 의존관계 있는 에이전트 → 순차 실행 (DoD 확인 후 다음 단계)
      4. 에이전트 간 KB 공유 상태로 진행 상황 동기화
      5. 실패 시 BACKWARD_JUMP 자동 처리
    """

    def __init__(self, board: str = "nova-loop"):
        self.board  = board
        self.kb_bus = KBBus()
        self.max_parallel = 4  # 동시 실행 최대 에이전트 수

    def _get_ready_tasks(self) -> list[dict]:
        """kanban ready 태스크 수집"""
        try:
            r = subprocess.run(
                ["hermes", "kanban", "--board", self.board, "list", "--json"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                tasks = json.loads(r.stdout)
                return [t for t in tasks if t.get("status") == "ready"]
        except Exception as e:
            log(f"kanban list 실패: {e}")
        return []

    def _get_parallel_agents(self, ready_tasks: list[dict]) -> list[list[str]]:
        """
        병렬 실행 가능한 에이전트 그룹 분류.
        
        Returns: [[병렬그룹1], [병렬그룹2], ...]
                 각 내부 리스트가 동시 실행 가능한 에이전트들
        """
        ready_agents = {t.get("assignee", "") for t in ready_tasks if t.get("assignee")}
        groups = []
        processed = set()

        # PARALLEL_SAFE 기반 병렬 그룹 구성
        for trigger, parallels in PARALLEL_SAFE.items():
            overlap = ready_agents & set(parallels)
            if len(overlap) >= 2:
                group = list(overlap)
                for a in group:
                    processed.add(a)
                groups.append(group)
                log(f"병렬 그룹 구성: {group}")

        # 나머지는 단독 실행
        for agent in ready_agents:
            if agent not in processed:
                groups.append([agent])

        return groups

    def _run_agent_group(self, agents: list[str], context: dict) -> dict[str, dict]:
        """에이전트 그룹을 병렬로 실행 (ThreadPoolExecutor)"""
        if len(agents) == 1:
            worker = AgentWorker(agents[0], context, self.kb_bus)
            result = worker.run()
            return {agents[0]: result}

        # 멀티에이전트 병렬 실행
        log(f"병렬 실행: {agents}")
        results = {}
        with ThreadPoolExecutor(max_workers=min(len(agents), self.max_parallel)) as pool:
            futures = {
                pool.submit(AgentWorker(a, {**context, "agent": a}, self.kb_bus).run): a
                for a in agents
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    result = future.result()
                    results[agent] = result
                    log(f"완료: {'OK' if result.get('ok') else 'FAIL'}", agent=agent)
                except Exception as e:
                    results[agent] = {"ok": False, "agent": agent, "output": str(e)}
                    log(f"예외 완료: {e}", agent=agent)

        return results

    def dispatch_ready(self) -> dict:
        """kanban의 ready 태스크를 멀티에이전트로 분산 실행"""
        ready_tasks = self._get_ready_tasks()
        if not ready_tasks:
            log("ready 태스크 없음")
            return {"dispatched": 0}

        log(f"ready 태스크 {len(ready_tasks)}개 발견")

        # 병렬 그룹 구성
        groups = self._get_parallel_agents(ready_tasks)
        total_dispatched = 0

        for group in groups:
            # 그룹의 태스크 정보 수집
            group_tasks = [t for t in ready_tasks if t.get("assignee") in group]
            context = {
                "topic": " / ".join(t.get("title", "")[:40] for t in group_tasks),
                "board": self.board,
            }

            # 실행
            results = self._run_agent_group(group, context)

            # 결과 처리
            for agent, result in results.items():
                task = next((t for t in group_tasks if t.get("assignee") == agent), None)
                if not task:
                    continue
                task_id = task.get("id", "")
                if result.get("ok"):
                    subprocess.run(
                        ["hermes", "kanban", "--board", self.board, "complete", task_id],
                        capture_output=True, text=True
                    )
                    log(f"태스크 완료: {task_id}", agent=agent)
                    total_dispatched += 1
                else:
                    subprocess.run(
                        ["hermes", "kanban", "--board", self.board, "block", task_id,
                         f"harness 실패: {result.get('output', '')[:80]}"],
                        capture_output=True, text=True
                    )
                    log(f"태스크 실패 → blocked: {task_id}", agent=agent)

        return {"dispatched": total_dispatched, "groups": len(groups)}

    def run_sprint(self, goal: str, max_rounds: int = 3) -> dict:
        """
        특정 목표에 대해 전체 스프린트를 멀티에이전트로 실행.
        kanban 태스크 자동 생성 → 병렬/직렬 실행 → KPI_PASS까지 반복.
        """
        log(f"스프린트 시작: {goal[:80]}")
        self.kb_bus.broadcast_status("orchestrator", "SPRINT_START", {"goal": goal})

        # nova-autoplan 태스크 생성
        try:
            subprocess.run(
                ["hermes", "kanban", "--board", self.board, "create",
                 "--title", f"[SPRINT] {goal[:60]}",
                 "--body", goal,
                 "--assignee", "nova-autoplan",
                 "--priority", "1"],
                capture_output=True, text=True, timeout=10
            )
        except Exception as e:
            log(f"태스크 생성 실패: {e}")

        # nudge brain_watcher
        try:
            eid = uuid.uuid4().hex[:16]
            now = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect(str(BRAIN_DB), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT OR IGNORE INTO hermes_events "
                "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eid, "SPRINT_START", "info", f"[SPRINT] {goal[:60]}", goal, "orchestrator", 0, now)
            )
            conn.execute("DELETE FROM hermes_events WHERE event_type='SPRINT_START'")
            conn.commit()
            conn.close()
            log("brain_watcher nudge 완료")
        except Exception as e:
            log(f"nudge 실패: {e}")

        return {"goal": goal, "status": "dispatched"}


# ── CLI 진입점 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NOVA 멀티에이전트 오케스트레이터")
    parser.add_argument("--board", default="nova-loop", help="kanban 보드명")
    parser.add_argument("--dispatch-ready", action="store_true", help="ready 태스크 분산 실행")
    parser.add_argument("--sprint", type=str, help="새 스프린트 목표")
    parser.add_argument("--agent", type=str, help="단일 에이전트 실행")
    parser.add_argument("--topic", type=str, default="", help="실행 주제/컨텍스트")
    parser.add_argument("--status", action="store_true", help="현재 상태 출력")
    args = parser.parse_args()

    orch = MultiAgentOrchestrator(board=args.board)

    if args.status:
        tasks = orch._get_ready_tasks()
        print(f"Board: {args.board}")
        print(f"Ready tasks: {len(tasks)}")
        for t in tasks:
            print(f"  [{t.get('assignee','')}] {t.get('title','')[:60]}")
        msgs = orch.kb_bus.read("*", mark_read=False)
        print(f"Agent messages: {len(msgs)}")

    elif args.dispatch_ready:
        result = orch.dispatch_ready()
        print(json.dumps(result, ensure_ascii=False))

    elif args.sprint:
        result = orch.run_sprint(args.sprint)
        print(json.dumps(result, ensure_ascii=False))

    elif args.agent:
        kb_bus = KBBus()
        worker = AgentWorker(args.agent, {"topic": args.topic or args.agent}, kb_bus)
        result = worker.run()
        print(json.dumps(result, ensure_ascii=False))
