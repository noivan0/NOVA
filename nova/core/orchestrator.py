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

import subprocess
import time
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
        workspace = Path(self.config.workspace) / harness.name
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

        print(
            f"\n[nova] {'Done' if success else 'Failed'}: {harness.name} "
            f"in {int(duration)}s — phases: {phases_run}"
        )
        return success

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
                phases_failed.append(phase.id)
                recovered = self._handle_failure(
                    phase, harness, workspace, context, result, runbook_fired
                )
                if not recovered:
                    if phase.on_failure == "skip":
                        print(f"[nova] Phase {phase.id} failed — skipping (on_failure=skip)")
                        continue
                    elif phase.on_failure == "abort":
                        print(f"[nova] Phase {phase.id} failed — aborting harness")
                        return False
                    else:
                        return False

            # Write output to workspace
            if result.output and phase.output_file:
                out = workspace / phase.output_file
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(result.output)

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
    ) -> bool:
        """
        In single-agent mode, fanout phases run sequentially then
        results are merged into context['_fanout_results'].
        FIX C3: start_phase respected so resume works for fanout harnesses.
        """
        fanout_results = {}

        for i, phase in enumerate(harness.phases):
            if i < start_phase:
                print(f"[nova] Skipping phase {i} ({phase.id}) — already done")
                phases_run.append(phase.id)
                continue

            # Serialize only primitive context values to checkpoint
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

            if result.success:
                fanout_results[phase.id] = result.output
                if result.output and phase.output_file:
                    out = workspace / phase.output_file
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(result.output)
                if result.quality_score is not None:
                    context["_last_quality_score"] = result.quality_score
            else:
                phases_failed.append(phase.id)
                if phase.on_failure == "abort":
                    print(f"[nova] Fanout phase {phase.id} failed — aborting (on_failure=abort)")
                    context["_fanout_results"] = fanout_results
                    return False
                else:
                    print(f"[nova] Fanout phase {phase.id} failed — continuing others")

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
                    if phase.quality_check and result.quality_score is not None:
                        if result.quality_score < self.config.quality_threshold:
                            print(
                                f"[nova]   quality={result.quality_score} < "
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
        output = self.llm.complete(prompt, system=system, timeout=timeout)

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
                cmd, shell=True, capture_output=True, text=True,
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
                    subprocess.run(rule.action, shell=True, cwd=str(workspace))
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
