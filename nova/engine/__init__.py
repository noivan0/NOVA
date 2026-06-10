"""
nova.engine — Pluggable engine scripts called by BrainWatcher.

BrainWatcher looks for engine scripts at $NOVA_HOME/engines/<name>.py.
If a script does not exist, that reaction is silently skipped.

Built-in engine keys and what they should do:

  dream        — "DreamCycle": consolidate knowledge, prune contradictions, reindex
  synthesize   — synthesize recent takes into structured KB pages
  learn        — lightweight learning pass: embed new takes, link to KB pages
  chain        — process completed kanban tasks and trigger next steps
  fix_orphan   — fix KB pages with no agent association
  memory_slim  — slim the agent memory file if it exceeds the size threshold

Custom engines
--------------
You can override any engine by passing the ``engines`` dict to ``run()``:

    from nova.watcher.brain import run

    run(
        nova_home=Path("~/.nova"),
        engines={
            "learn":     ["python", "/my/project/engines/learn.py"],
            "dream":     ["bash", "/my/project/engines/dream.sh"],
            "synthesize": ["python", "-m", "myproject.synthesize"],
        },
    )

Engine script contract
----------------------
Each engine script is called as a subprocess. It must:
  - Exit 0 on success, non-zero on failure.
  - Write output to stdout (logged by BrainWatcher).
  - Be idempotent (safe to run multiple times).
  - Use $NOVA_HOME or accept --nova-home argument for data paths.

See examples/engines/ for minimal reference implementations.
"""
