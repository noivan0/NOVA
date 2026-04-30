"""
nova/cli/main.py
----------------
NOVA CLI — command-line interface for running and managing harnesses.

Commands:
  nova run <harness>           Run a harness
  nova run <harness> --resume  Resume from checkpoint
  nova run <harness> --dry-run Preview without LLM calls
  nova list                    List available harnesses
  nova status <harness>        Show checkpoint / evolution status
  nova evolution <harness>     Show evolution log
  nova kb search <query>       Search the knowledge base
  nova kb write <key> <file>   Write a file into the KB
  nova new <name>              Scaffold a new harness
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nova",
        description="NOVA — AI Orchestration Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="Run a harness")
    run_p.add_argument("harness", help="Harness name or path to harness.yaml")
    run_p.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    run_p.add_argument("--dry-run", action="store_true", help="Dry run (no LLM calls)")
    run_p.add_argument("--config", default="nova.yaml", help="Config file path")
    run_p.add_argument("--context", nargs="*", help="Extra context as key=value pairs")

    # --- list ---
    list_p = sub.add_parser("list", help="List available harnesses")
    list_p.add_argument("--config", default="nova.yaml", help="Config file path")

    # --- status ---
    status_p = sub.add_parser("status", help="Show harness checkpoint and status")
    status_p.add_argument("harness", help="Harness name")
    status_p.add_argument("--config", default="nova.yaml", help="Config file path")

    # --- evolution ---
    evo_p = sub.add_parser("evolution", help="Show evolution log")
    evo_p.add_argument("harness", help="Harness name")
    evo_p.add_argument("--last", type=int, default=5, help="Show last N entries")
    evo_p.add_argument("--config", default="nova.yaml", help="Config file path")

    # --- kb ---
    kb_p = sub.add_parser("kb", help="Knowledge base operations")
    kb_p.add_argument("--config", default="nova.yaml", help="Config file path")
    kb_sub = kb_p.add_subparsers(dest="kb_command", required=True)

    kb_search = kb_sub.add_parser("search", help="Search the KB")
    kb_search.add_argument("query", help="Search query")

    kb_write = kb_sub.add_parser("write", help="Write a file into the KB")
    kb_write.add_argument("key", help="KB key (e.g. projects/my-harness)")
    kb_write.add_argument("file", help="File to read from")

    kb_list = kb_sub.add_parser("list", help="List KB pages")
    kb_list.add_argument("--prefix", default="", help="Filter by prefix")

    # --- new ---
    new_p = sub.add_parser("new", help="Scaffold a new harness")
    new_p.add_argument("name", help="Harness name (lowercase-hyphenated)")
    new_p.add_argument("--config", default="nova.yaml", help="Config file path")
    new_p.add_argument("--pattern", default="pipeline",
                       choices=["pipeline", "fanout", "supervisor", "generative"],
                       help="Execution pattern")

    args = parser.parse_args()

    # Lazy import to keep startup fast
    from nova.core.config import load_config
    from nova.core.harness import HarnessLoader
    from nova.core.checkpoint import Checkpoint
    from nova.core.evolution import EvolutionLog
    from nova.core.kb import KB
    from nova.core.orchestrator import Orchestrator

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    if args.command == "run":
        cfg = load_config(args.config)
        if args.dry_run:
            cfg.dry_run = True

        loader = HarnessLoader(cfg.harnesses_dir)

        # Support direct path or name
        if Path(args.harness).exists():
            harness = loader.load_from_file(args.harness)
        else:
            harness = loader.load(args.harness)

        # Parse extra context
        context = {}
        for kv in (args.context or []):
            if "=" in kv:
                k, v = kv.split("=", 1)
                context[k] = v

        orch = Orchestrator(cfg)
        ok = orch.run(harness, context=context, resume=args.resume)
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------------------ #
    # list
    # ------------------------------------------------------------------ #
    elif args.command == "list":
        cfg = load_config(args.config)
        loader = HarnessLoader(cfg.harnesses_dir)
        harnesses = loader.list_harnesses()

        if not harnesses:
            print("No harnesses found in:", cfg.harnesses_dir)
        else:
            print(f"Available harnesses ({cfg.harnesses_dir}):")
            for name in harnesses:
                try:
                    h = loader.load(name)
                    print(f"  {name:<25} [{h.pattern}] {h.description[:60]}")
                except Exception as e:
                    print(f"  {name:<25} [error: {e}]")

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #
    elif args.command == "status":
        cfg = load_config(args.config)
        workspace = Path(cfg.workspace) / args.harness
        ckpt = Checkpoint(str(workspace))
        saved = ckpt.resume()

        if saved:
            print(f"Harness:  {saved['harness']}")
            print(f"Run ID:   {saved['run_id']}")
            print(f"Phase:    {saved['phase']} ({saved['phase_id']})")
            print(f"Started:  {saved['started_at']}")
            print(f"Phase at: {saved['phase_started_at']}")
        else:
            print(f"No active checkpoint for '{args.harness}'")

    # ------------------------------------------------------------------ #
    # evolution
    # ------------------------------------------------------------------ #
    elif args.command == "evolution":
        cfg = load_config(args.config)
        workspace = Path(cfg.workspace) / args.harness
        evo = EvolutionLog(str(workspace))
        entries = evo.recent(args.last)

        if not entries:
            print(f"No evolution data for '{args.harness}'")
        else:
            failure_rate = evo.failure_rate(len(entries))
            print(f"Evolution log — {args.harness} (last {len(entries)} runs, "
                  f"failure rate: {failure_rate:.0%})")
            print("-" * 60)
            for e in reversed(entries):
                status = "OK" if e["success"] else "FAIL"
                score = f"  q={e['quality_score']}" if e["quality_score"] else ""
                print(f"  [{status}] {e['run_id']}  {e['duration_secs']}s{score}")
                if e["phases_failed"]:
                    print(f"         failed: {e['phases_failed']}")

    # ------------------------------------------------------------------ #
    # kb
    # ------------------------------------------------------------------ #
    elif args.command == "kb":
        cfg = load_config(args.config)
        kb = KB(cfg.kb.path)

        if args.kb_command == "search":
            results = kb.search(args.query)
            if not results:
                print(f"No results for '{args.query}'")
            else:
                for r in results:
                    print(f"  {r['key']}:{r['line_number']}  {r['line'][:100]}")

        elif args.kb_command == "write":
            content = Path(args.file).read_text()
            path = kb.write(args.key, content)
            print(f"Written: {path}")

        elif args.kb_command == "list":
            pages = kb.list_pages(prefix=args.prefix)
            for p in pages:
                print(f"  {p}")

    # ------------------------------------------------------------------ #
    # new
    # ------------------------------------------------------------------ #
    elif args.command == "new":
        cfg = load_config(args.config)
        _scaffold_harness(args.name, args.pattern, cfg.harnesses_dir)


def _scaffold_harness(name: str, pattern: str, harnesses_dir: str) -> None:
    """Generate a minimal harness skeleton."""
    d = Path(harnesses_dir) / name
    if d.exists():
        print(f"Error: harness '{name}' already exists at {d}")
        sys.exit(1)

    (d / "prompts").mkdir(parents=True)
    (d / "agents").mkdir()

    harness_yaml = f"""\
name: {name}
description: "{name} harness"
version: "1.0.0"
pattern: {pattern}

# persona: describe your target user here (optional but recommended)
# persona: "A professional blogger who publishes weekly travel content."

phases:
  - id: step_1
    name: "Step 1"
    description: "First phase — customize this"
    executor: llm
    prompt_file: step_1.txt
    output_file: step_1_output.md
    quality_check: false
    on_failure: retry

  - id: step_2
    name: "Step 2"
    description: "Second phase — customize this"
    executor: llm
    input_files:
      - step_1_output.md
    prompt_file: step_2.txt
    output_file: final_output.md
    quality_check: false
    on_failure: abort

runbook:
  - symptom: "rate limit"
    action: "wait:60"
    escalate_after: 3600

evolution:
  enabled: true
  file: evolution.md
"""

    step1_prompt = """\
You are an expert assistant.

Task: Complete the first step of the {name} workflow.

Context: {{_context}}

Output a detailed, well-structured response.
""".format(name=name)

    step2_prompt = """\
You are an expert assistant.

Previous step output:
{{step_1_output.md}}

Task: Complete the second step using the above context.

Output a final, polished result.
"""

    (d / "harness.yaml").write_text(harness_yaml)
    (d / "prompts" / "step_1.txt").write_text(step1_prompt)
    (d / "prompts" / "step_2.txt").write_text(step2_prompt)
    (d / "evolution.md").write_text(f"# Evolution Log — {name}\n")

    print(f"Harness scaffolded: {d}")
    print(f"  Edit: {d}/harness.yaml")
    print(f"  Prompts: {d}/prompts/")
    print(f"  Run: nova run {name}")


if __name__ == "__main__":
    main()
