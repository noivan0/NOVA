"""
nova/core/orchestrator.py
--------------------------
NOVA Orchestrator — the central execution engine.

Responsibilities:
  1. Load harness definition
  2. Restore from checkpoint (if resuming)
  3. Execute phases in order (pipeline) or parallel (fanout)
  4. Apply quality gate between phases
  5. Trigger RunBook rules on failure
  6. Record evolution log on completion
  7. Notify via configured notifier

Single-agent design:
  Every phase runs within the same process using the configured
  LLM provider. There is no secondary agent, IPC, or SSH required.
  Phase executors: llm | shell | python | passthrough
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from nova.core.checkpoint import Checkpoint
from nova.core.config import NOVAConfig
from nova.core.evolution import EvolutionLog
from nova.core.harness import HarnessDefinition, PhaseDefinition
from nova.core.kb import KB
from nova.providers.llm import get_llm_provider
from nova.providers.notifier import get_notifier
from nova.providers.publisher import get_publisher


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """SECURITY-002: 원자적 파일 쓰기 — 중간 읽기로 인한 불완전 데이터 노출 방지.
    임시 파일에 먼저 쓴 후 os.replace()로 원자적으로 교체.
    POSIX에서 os.replace()는 atomic이므로 경쟁 조건(race condition) 없음.
    수정일: 2026-07-19 / 수정자: nova-dev (t_01323546)
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class PhaseResult:
    def __init__(
        self,
        phase_id: str,
        success: bool,
        output: str = "",
        quality_score: Optional[int] = None,
        error: str = "",
    ):
        self.phase_id = phase_id
        self.success = success
        self.output = output
        self.quality_score = quality_score
        self.error = error


class Orchestrator:
    def __init__(self, config: NOVAConfig):
        self.config = config
        self.llm = get_llm_provider(config.llm)
        self.notifier = get_notifier(config.notifier)
        # FIX M4: instantiate publisher at orchestrator level so harness python
        # phases can access it via context["_publisher"] without importing nova internals
        self.publisher = get_publisher(config.publisher)
        self.kb = KB(config.kb.path)

    def run(
        self,
        harness: HarnessDefinition,
        context: Optional[Dict[str, Any]] = None,
        resume: bool = True,
    ) -> bool:
        """
        Execute a harness. Returns True if all phases succeeded.

        Args:
            harness: Loaded HarnessDefinition
            context: Optional dict injected into all LLM phase prompts
            resume: If True, attempt to resume from checkpoint
        """
        workspace = Path(self.config.workspace).expanduser() / harness.name
        workspace.mkdir(parents=True, exist_ok=True)

        checkpoint = Checkpoint(str(workspace))
        evolution = EvolutionLog(str(workspace))

        # Attempt resume
        start_phase = 0
        run_id: Optional[str] = None
        saved = checkpoint.resume() if resume else None

        if saved:
            start_phase = saved["phase"]
            run_id = saved["run_id"]
            print(f"[nova] Resuming {harness.name} from phase {start_phase} ({saved['phase_id']})")
        else:
            run_id = checkpoint.start(harness.name, stale_threshold_secs=self.config.phase_timeout)
            print(f"[nova] Starting {harness.name} — run_id={run_id}")

        started_at = _now()
        t0 = time.monotonic()

        phases_run: List[str] = []
        phases_failed: List[str] = []
        runbook_fired: List[str] = []
        final_quality: Optional[int] = None

        # FIX M4: inject publisher into context so python phases don't need to import nova
        ctx = context or {}
        ctx.setdefault("_publisher", self.publisher)

        # Dispatch by pattern
        if harness.pattern in ("pipeline", "generative", "supervisor"):
            success = self._run_pipeline(
                harness, workspace, checkpoint, ctx,
                start_phase, phases_run, phases_failed, runbook_fired,
            )
        elif harness.pattern == "fanout":
            success = self._run_fanout(
                harness, workspace, checkpoint, ctx,
                start_phase, phases_run, phases_failed,
                runbook_fired=runbook_fired,  # BUG-3 수정
            )
        else:
            print(f"[nova] Unknown pattern '{harness.pattern}', defaulting to pipeline")
            success = self._run_pipeline(
                harness, workspace, checkpoint, ctx,
                start_phase, phases_run, phases_failed, runbook_fired,
            )

        duration = time.monotonic() - t0
        checkpoint.complete()

        # FIX H5: collect final quality score from last scored phase
        # (quality_score on individual phases is tracked per-run in phases_run context)
        final_quality = ctx.get("_last_quality_score")

        # Record evolution
        if harness.evolution.enabled:
            evolution.record(
                run_id=run_id,
                harness=harness.name,
                pattern=harness.pattern,
                started_at=started_at,
                success=success,
                duration_secs=duration,
                quality_score=final_quality,
                phases_run=phases_run,
                phases_failed=phases_failed,
                runbook_fired=runbook_fired,
            )

        # Auto-alert on consecutive failures
        if not success:
            consecutive = evolution.consecutive_failures()
            if consecutive >= 3:
                self.notifier.send(
                    f"[NOVA] {harness.name}: {consecutive} consecutive failures. "
                    f"Last failed phases: {phases_failed}"
                )

        # KB log
        status_str = "SUCCESS" if success else "FAILURE"
        self.kb.append_log(
            f"harness-run | {harness.name} — {status_str} "
            f"({len(phases_run)} phases, {int(duration)}s)"
        )

        # workspace output → brain.db 자동 인덱싱
        if success:
            try:
                self._index_workspace_output(harness.name, workspace)
            except Exception as _ie:
                pass  # 인덱싱 실패는 무시

        print(
            f"\n[nova] {'Done' if success else 'Failed'}: {harness.name} "
            f"in {int(duration)}s — phases: {phases_run}"
        )
        return success

    def _index_workspace_output(self, harness_name: str, workspace: Path) -> None:
        """harness workspace의 report.md / output.md를 brain.db pages에 자동 인덱싱."""
        import sqlite3, hashlib
        from datetime import datetime, timezone

        BRAIN_DB = Path(self.config.workspace).expanduser().parent / "brain.db"
        if not BRAIN_DB.exists():
            return

        targets = ["report.md", "output.md", "synthesis.md", "implementation.md"]
        for fname in targets:
            fpath = workspace / fname
            if not fpath.exists():
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if len(text) < 50:
                continue

            path_key     = f"workspace/{harness_name}/{fname}"
            title_prefix = (text.lstrip("#").split(chr(10))[0].strip() or f"[{harness_name}]")[:80]
            # SECURITY-INT-002: usedforsecurity=False — MD5 is for content-dedup indexing only, not auth/crypto
            content_hash = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
            now          = datetime.now(timezone.utc).isoformat()

            try:
                with sqlite3.connect(str(BRAIN_DB)) as con:
                    con.execute("PRAGMA journal_mode=WAL")
                    page_id = hashlib.sha256(path_key.encode()).hexdigest()[:16]
                    con.execute(
                        """INSERT INTO pages
                               (id, path, title, page_type, compiled_truth, char_count,
                                content_hash, indexed_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET
                               title=excluded.title, compiled_truth=excluded.compiled_truth,
                               char_count=excluded.char_count, content_hash=excluded.content_hash,
                               indexed_at=excluded.indexed_at, updated_at=excluded.updated_at""",
                        (page_id, path_key, title_prefix, "workspace",
                         text[:3000], len(text), content_hash, now, now)
                    )
                    con.commit()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Pipeline execution
    # ------------------------------------------------------------------ #

    def _run_pipeline(
        self,
        harness: HarnessDefinition,
        workspace: Path,
        checkpoint: Checkpoint,
        context: Dict[str, Any],
        start_phase: int,
        phases_run: List[str],
        phases_failed: List[str],
        runbook_fired: List[str],
    ) -> bool:
        for i, phase in enumerate(harness.phases):
            if i < start_phase:
                print(f"[nova] Skipping phase {i} ({phase.id}) — already done")
                phases_run.append(phase.id)
                continue

            # Serialize only string/primitive context values to checkpoint
            # (objects like _publisher are non-serializable and rebuilt at startup)
            serializable_ctx = {
                k: v
                for k, v in context.items()
                if not k.startswith("_")
                and isinstance(
                    v,
                    (str, int, float, bool, list, dict, type(None)),
                )
            }
            checkpoint.update(i, phase.id, {"context": serializable_ctx})
            result = self._execute_phase(phase, workspace, context, harness)
            phases_run.append(phase.id)

            if not result.success:
                recovered = self._handle_failure(
                    phase, harness, workspace, context, result, runbook_fired
                )
                if not recovered:
                    if phase.on_failure == "skip":
                        print(f"[nova] Phase {phase.id} failed — skipping (on_failure=skip)")
                        # skip 실패는 phases_failed에 추가하지 않음 — fanout과 동일 동작
                        continue
                    phases_failed.append(phase.id)  # skip이 아닌 경우에만 추가
                    if phase.on_failure == "abort":
                        print(f"[nova] Phase {phase.id} failed — aborting harness")
                        return False
                    else:
                        return False

            # Write output to workspace — SECURITY-002: 원자적 쓰기 (race condition 방지)
            if result.output and phase.output_file:
                out = workspace / phase.output_file
                out.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(out, result.output)

            # Update context for next phase
            context[f"_phase_{phase.id}"] = result.output
            # Track last quality score for evolution log (FIX H5)
            if result.quality_score is not None:
                context["_last_quality_score"] = result.quality_score

        return True

    # ------------------------------------------------------------------ #
    # Fanout execution (parallel-style, sequential in single-agent mode)
    # ------------------------------------------------------------------ #

    def _run_fanout(
        self,
        harness: HarnessDefinition,
        workspace: Path,
        checkpoint: Checkpoint,
        context: Dict[str, Any],
        start_phase: int,
        phases_run: List[str],
        phases_failed: List[str],
        runbook_fired: Optional[List[str]] = None,  # BUG-3 수정: runbook 전달
    ) -> bool:
        """Fanout: skip-able phases run in parallel, abort phases run sequentially.
        Groups of consecutive skip phases execute concurrently via ThreadPoolExecutor.
        Abort phases always run sequentially (depend on prior phase output).
        FIX C3: start_phase respected so resume works for fanout harnesses.
        """
        fanout_results: Dict[str, str] = {}
        lock = threading.Lock()

        def _run_one(i: int, phase: PhaseDefinition) -> tuple[int, PhaseDefinition, "PhaseResult"]:
            serializable_ctx = {
                k: v for k, v in context.items()
                if not k.startswith("_")
                and isinstance(v, (str, int, float, bool, list, dict, type(None)))
            }
            checkpoint.update(i, phase.id, {"context": serializable_ctx})
            return i, phase, self._execute_phase(phase, workspace, context, harness)

        # 페이즈를 순차/병렬 그룹으로 분류
        # skip 페이즈 연속 그룹 → ThreadPool 병렬, abort 페이즈 → 단독 순차
        phases_to_run = [
            (i, ph) for i, ph in enumerate(harness.phases) if i >= start_phase
        ]

        i = 0
        while i < len(phases_to_run):
            idx, phase = phases_to_run[i]

            # abort/abort_noretry 페이즈는 단독 순차 실행
            if phase.on_failure in ("abort", "abort_noretry"):
                _, _, result = _run_one(idx, phase)
                phases_run.append(phase.id)
                if result.success:
                    fanout_results[phase.id] = result.output or ""
                    if result.output and phase.output_file:
                        out = workspace / phase.output_file
                        out.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write(out, result.output)  # SECURITY-002: 원자적 쓰기
                    if result.quality_score is not None:
                        context["_last_quality_score"] = result.quality_score
                else:
                    phases_failed.append(phase.id)
                    print(f"[nova] Fanout phase {phase.id} failed — aborting (on_failure=abort)")
                    if runbook_fired is not None:
                        self._handle_failure(phase, harness, workspace, context, result, runbook_fired)
                    context["_fanout_results"] = fanout_results
                    return False
                i += 1

            else:
                # skip 페이즈 연속 그룹 수집 → 병렬 실행
                skip_group: List[tuple[int, PhaseDefinition]] = []
                j = i
                while j < len(phases_to_run):
                    _, ph = phases_to_run[j]
                    if ph.on_failure not in ("abort", "abort_noretry"):
                        skip_group.append(phases_to_run[j])
                        j += 1
                    else:
                        break

                max_workers = min(len(skip_group), 4)  # 최대 4개 병렬
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_run_one, gi, gph): (gi, gph) for gi, gph in skip_group}
                    for fut in as_completed(futures):
                        try:
                            _, ph, result = fut.result()
                            phases_run.append(ph.id)
                            if result.success:
                                with lock:
                                    fanout_results[ph.id] = result.output or ""
                                if result.output and ph.output_file:
                                    out = workspace / ph.output_file
                                    out.parent.mkdir(parents=True, exist_ok=True)
                                    _atomic_write(out, result.output)  # SECURITY-002: 원자적 쓰기
                                if result.quality_score is not None:
                                    with lock:  # BUG-W8a: race condition 수정 - lock 추가
                                        context["_last_quality_score"] = result.quality_score
                            else:
                                print(f"[nova] Fanout phase {ph.id} failed — skipping")
                        except Exception as e:
                            print(f"[nova] Fanout phase exception: {e}")
                i = j  # 처리한 그룹 다음으로

        context["_fanout_results"] = fanout_results
        return len(phases_failed) == 0

    # ------------------------------------------------------------------ #
    # Phase executor dispatch
    # ------------------------------------------------------------------ #

    def _execute_phase(
        self,
        phase: PhaseDefinition,
        workspace: Path,
        context: Dict[str, Any],
        harness: HarnessDefinition,
    ) -> PhaseResult:
        timeout = phase.timeout or self.config.phase_timeout
        max_retries = phase.retries if phase.retries is not None else self.config.max_retries

        print(f"\n[nova] Phase: {phase.id} ({phase.executor}) timeout={timeout}s")

        result: PhaseResult = PhaseResult(phase.id, False, error="No attempts made")
        for attempt in range(max_retries + 1):
            if attempt > 0:
                print(f"[nova]   retry {attempt}/{max_retries}")

            try:
                if phase.executor == "llm":
                    result = self._exec_llm(phase, workspace, context, harness, timeout)
                elif phase.executor == "shell":
                    result = self._exec_shell(phase, workspace, context, timeout)
                elif phase.executor == "python":
                    result = self._exec_python(phase, workspace, context, timeout)
                elif phase.executor == "passthrough":
                    result = PhaseResult(phase.id, True, output="")
                else:
                    result = PhaseResult(
                        phase.id,
                        False,
                        error=f"Unknown executor: {phase.executor}",
                    )

                if result.success:
                    # Optional quality gate
                    if phase.quality_check:
                        score = result.quality_score
                        if score is None:
                            # LLM이 스코어 패턴을 반환 안 하면 임계값 미달로 처리
                            print(f"[nova]   quality_score 미파싱 — threshold 미달로 처리")
                            score = 0
                        if score < self.config.quality_threshold:
                            print(
                                f"[nova]   quality={score} < "
                                f"threshold={self.config.quality_threshold} — retrying"
                            )
                            if attempt < max_retries:
                                continue
                            result.success = False
                    return result

            except Exception as e:
                result = PhaseResult(phase.id, False, error=str(e))
                print(f"[nova]   exception: {e}")

            if attempt >= max_retries:
                break

        return result

    def _exec_llm(
        self,
        phase: PhaseDefinition,
        workspace: Path,
        context: Dict[str, Any],
        harness: HarnessDefinition,
        timeout: int,
    ) -> PhaseResult:
        # Build prompt: interpolate context variables
        prompt = phase.prompt
        for k, v in context.items():
            prompt = prompt.replace(f"{{{{{k}}}}}", str(v))

        # Inject input file contents — both appended AND available as {{ filename }} template vars
        inputs = {}
        for fname in phase.input_files:
            fpath = workspace / fname
            if fpath.exists():
                inputs[fname] = fpath.read_text()

        # Template substitution for {{ filename }} in prompt
        for fname, content in inputs.items():
            prompt = prompt.replace(f"{{{{{fname}}}}}", content)

        # Append any remaining input files not already substituted
        unsubstituted = {
            fn: ct for fn, ct in inputs.items()
            if f"{{{{{fn}}}}}" not in phase.prompt  # already handled above
        }
        if unsubstituted:
            files_block = "\n\n".join(
                f"=== {fn} ===\n{content}" for fn, content in unsubstituted.items()
            )
            prompt = f"{prompt}\n\n{files_block}"

        if self.config.dry_run:
            print(f"[nova][dry-run] LLM prompt ({len(prompt)} chars)")
            return PhaseResult(phase.id, True, output=f"[dry-run] phase={phase.id}")

        # Pass harness persona as system prompt
        system = harness.persona or ""

        # per-phase LLM 오버라이드: phase에 provider/model 지정 시 동적 인스턴스 생성
        if phase.provider:
            from nova.core.config import LLMConfig
            p = phase.provider.lower()
            # codex/openai provider → config.codex 설정 우선 폴백 (BUG-1 수정)
            if p in ("codex", "openai") and hasattr(self.config, "codex"):
                fallback = self.config.codex
                phase_llm_cfg = LLMConfig(
                    provider=phase.provider,
                    model=phase.model or fallback.model,
                    api_key=phase.api_key or fallback.api_key or self.config.llm.api_key,
                    base_url=phase.base_url or fallback.base_url,
                )
            else:
                phase_llm_cfg = LLMConfig(
                    provider=phase.provider,
                    model=phase.model or self.config.llm.model,
                    api_key=phase.api_key or self.config.llm.api_key,
                    base_url=phase.base_url or self.config.llm.base_url,
                )
            llm = get_llm_provider(phase_llm_cfg)
            print(f"[nova] Phase {phase.id}: provider={phase.provider} model={phase_llm_cfg.model}")
        else:
            llm = self.llm

        output = llm.complete(prompt, system=system, timeout=timeout)

        # Parse quality score from LLM output when quality_check is set
        quality_score: Optional[int] = None
        if phase.quality_check:
            quality_score = _parse_quality_score(output)

        return PhaseResult(phase.id, True, output=output, quality_score=quality_score)

    def _exec_shell(
        self,
        phase: PhaseDefinition,
        workspace: Path,
        context: Dict[str, Any],
        timeout: int,
    ) -> PhaseResult:
        cmd = phase.command
        for k, v in context.items():
            cmd = cmd.replace(f"{{{{{k}}}}}", str(v))

        if self.config.dry_run:
            print(f"[nova][dry-run] shell: {cmd}")
            return PhaseResult(phase.id, True, output="[dry-run]")

        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,  # nosec B602 — harness-defined cmd only, no user input
                timeout=timeout, cwd=str(workspace)
            )
            success = proc.returncode == 0
            output = proc.stdout + proc.stderr
            return PhaseResult(phase.id, success, output=output, error=proc.stderr)
        except subprocess.TimeoutExpired:
            return PhaseResult(phase.id, False, error=f"Shell command timed out after {timeout}s")

    def _exec_python(
        self,
        phase: PhaseDefinition,
        workspace: Path,
        context: Dict[str, Any],
        timeout: int = 120,
    ) -> PhaseResult:
        """Execute inline Python code defined in phase.command."""
        import signal

        local_vars: Dict[str, Any] = {"workspace": workspace, "context": context, "output": ""}

        def _timeout_handler(signum: int, frame: object) -> None:
            raise TimeoutError(f"Python phase '{phase.id}' timed out after {timeout}s")

        old_handler = None
        use_signal = hasattr(signal, "SIGALRM")  # SIGALRM not available on Windows
        try:
            if use_signal:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(timeout)
            exec(phase.command, {"__builtins__": __builtins__}, local_vars)  # noqa: S102
            return PhaseResult(phase.id, True, output=str(local_vars.get("output", "")))
        except TimeoutError as e:
            return PhaseResult(phase.id, False, error=str(e))
        except Exception as e:
            return PhaseResult(phase.id, False, error=str(e))
        finally:
            if use_signal:
                signal.alarm(0)
                if old_handler is not None:
                    signal.signal(signal.SIGALRM, old_handler)

    # ------------------------------------------------------------------ #
    # Failure handling
    # ------------------------------------------------------------------ #

    def _handle_failure(
        self,
        phase: PhaseDefinition,
        harness: HarnessDefinition,
        workspace: Path,
        context: Dict[str, Any],
        result: PhaseResult,
        runbook_fired: List[str],
    ) -> bool:
        """Apply RunBook rules. Returns True if the phase should be considered recovered."""
        for rule in harness.runbook:
            if rule.symptom.lower() in result.error.lower():
                print(f"[nova] RunBook match: symptom='{rule.symptom}' action='{rule.action}'")
                runbook_fired.append(rule.symptom)

                if rule.action.startswith("wait:"):
                    secs = int(rule.action.split(":")[1])
                    print(f"[nova] RunBook: waiting {secs}s before escalating")
                    time.sleep(min(secs, 10))  # cap at 10s in practice
                    return False
                elif rule.action == "notify":
                    self.notifier.send(
                        f"[NOVA RunBook] {harness.name}/{phase.id}: {result.error}"
                    )
                    return False
                else:
                    # Treat action as a shell command
                    subprocess.run(rule.action, shell=True, cwd=str(workspace))  # nosec B602 — runbook action, harness-controlled
                    return False

        self.notifier.send(
            f"[NOVA] {harness.name}/{phase.id} failed: {result.error[:200]}"
        )
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_quality_score(text: str) -> Optional[int]:
    """
    FIX C2: Extract a numeric quality score (0-100) from LLM output.

    Looks for patterns like:
      SCORE: 85
      Quality: 72/100
      quality_score: 90
      [SCORE=88]
    Returns None if no score found (gate is skipped, not failed).
    """
    import re
    patterns = [
        r"(?:SCORE|quality[_\s]?score|score)\s*[:=]\s*(\d{1,3})",
        r"(\d{1,3})\s*/\s*100",
        r"\[SCORE=(\d{1,3})\]",
        r"(\d{1,3})\s*(?:out of|\/)\s*100",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return val
    return None
