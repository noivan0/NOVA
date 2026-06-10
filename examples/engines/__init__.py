"""
examples/engines/ — Reference engine implementations for nova.watcher.brain.

BrainWatcher looks for engines at $NOVA_HOME/engines/<name>.py.
Copy any of these to ~/.nova/engines/ and customise to your needs.

Available examples:
  learn.py      — lightweight learning pass (+5 takes threshold)
  synthesize.py — knowledge synthesis into KB pages (+15 takes threshold)
  dream.py      — full DreamCycle: consolidate, score health (+100 takes or health < 90)

To wire an engine to BrainWatcher, either:

  1. Copy to $NOVA_HOME/engines/<name>.py (auto-discovered), or

  2. Pass explicitly via the ``engines`` parameter:

        from nova.watcher.brain import run
        from pathlib import Path

        run(
            nova_home=Path("~/.nova"),
            engines={
                "learn":     ["python", "examples/engines/learn.py"],
                "synthesize": ["python", "examples/engines/synthesize.py"],
                "dream":     ["python", "examples/engines/dream.py"],
            },
        )
"""
