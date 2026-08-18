# Quickstart Guide

Get NOVA running in 5 minutes — harness pipelines, autonomous watchers, and all.

---

## Prerequisites

- Python 3.10+
- Linux (for watchers — requires `inotifywait`). macOS/Windows: harnesses work fine; watchers need inotify-tools or an equivalent.
- An LLM API key, **or** use the built-in `echo` provider (no key — great for testing)

---

## Step 1: Install

> **Not on PyPI yet.** `nova-orchestrator` has not been published, so
> `pip install nova-orchestrator` will fail with "No matching distribution
> found". Until it is published, install directly from GitHub:

```bash
# No API key needed to try it — echo provider works out of the box
pip install "nova-orchestrator[openai] @ git+https://github.com/noivan0/NOVA.git"

# Or clone + editable install (recommended for development)
git clone https://github.com/noivan0/NOVA.git
cd NOVA
pip install -e ".[openai]"     # swap [openai] for [anthropic] / [ollama] / [all] / omit for echo-only
```

Verify:

```bash
nova --version
# nova 1.4.0
```

### Try it immediately — zero config, zero API key

`pip install`/`pip install -e .` does **not** require a `git clone`'s
`harnesses/` folder anymore — example harnesses ship inside the package
itself and `nova` falls back to them automatically:

```bash
nova list
# Available harnesses (./harnesses):
#   research [fanout] ...
#   summarizer [pipeline] ...
#   data-pipeline [pipeline] ...
#   ... (21 built-in harnesses)

NOVA_LLM_PROVIDER=echo nova run research --context topic="test" --dry-run
# [nova] Starting research — run_id=...
# ... completes without any API key or git clone
```

Want your own editable copy of the example harnesses + a starter `nova.yaml`
in the current directory? Run:

```bash
nova init                 # copies research, summarizer, data-pipeline + nova.yaml
nova init --all           # copies all 21 bundled harnesses
```

---

## Step 2: Initialize your NOVA home

```bash
nova setup
```

This creates `~/.nova/` with:

```
~/.nova/
├── brain.db          ← SQLite knowledge store (pages, takes, events)
├── memory.md         ← Persistent agent memory
├── kb/               ← Knowledge base markdown files
│   ├── lessons/
│   └── synthesis/
├── wiki/             ← Auto-generated wiki pages
│   ├── entities/
│   └── concepts/
├── kanban/boards/    ← Task tracking (optional)
├── engines/          ← Built-in reaction engines (auto-installed)
│   ├── dream.py
│   ├── learn.py
│   ├── synthesize.py
│   ├── chain.py
│   ├── fix_orphan.py
│   └── memory_slim.py
└── logs/             ← Watcher logs and pid files
```

Use a custom location:

```bash
nova setup --nova-home ~/my-project/nova
export NOVA_HOME=~/my-project/nova   # add to .bashrc / .zshrc
```

---

## Step 3: Start the autonomous watchers (Linux only)

Install inotify-tools if you haven't already:

```bash
sudo apt-get install inotify-tools   # Debian/Ubuntu
sudo dnf install inotify-tools       # Fedora/RHEL
```

Start all watchers:

```bash
nova watcher start
```

Check they're running:

```bash
nova watcher status
# NOVA watcher status (nova_home=~/.nova)
#
#   [brain-watcher] RUNNING (pid=12345)
#   [kb-watcher]    RUNNING (pid=12346)
#   [hook-server]   RUNNING (pid=12347)
#
#   brain.db: takes=0 orphan=0 health=100.0
```

Stop when you're done:

```bash
nova watcher stop
```

> **macOS / Windows**: skip this step. Harnesses still run and accumulate knowledge — you just
> trigger the engines manually: `python -m nova.engine.learn`, `python -m nova.engine.dream`, etc.

---

## Step 4: Run a harness

### No API key (echo provider)

```bash
nova run research --provider echo --context topic="transformer attention mechanisms"
```

### With OpenAI

```bash
NOVA_LLM_API_KEY=sk-... nova run research --context topic="transformer attention mechanisms"
```

### With Anthropic

```bash
NOVA_LLM_API_KEY=sk-ant-... nova run research \
  --provider anthropic --context topic="transformer attention mechanisms"
```

### With local Ollama

```bash
# Start Ollama first: ollama serve
nova run research --provider ollama --context topic="transformer attention mechanisms"
```

The run produces:
- `~/.nova/output/research/report.md` — final output
- `~/.nova/output/research/evolution.md` — quality score history
- New **takes** in `brain.db` — picked up by watchers automatically

---

## Step 5: Watch the autonomous loop

After a few harness runs, the watchers react automatically:

```
takes +5   → learn engine runs  → links takes to KB pages
takes +15  → synthesize runs    → writes kb/synthesis/nova-*.md
takes +100 → DreamCycle runs    → computes health score, emits DREAM_DONE event
```

Check what happened:

```bash
nova watcher status
# brain.db: takes=18 orphan=0 health=97.5
```

Read synthesized knowledge:

```bash
ls ~/.nova/kb/synthesis/
# nova-2026-06-10.md
```

---

## Step 6: Add your own knowledge

You can add knowledge directly — the watchers will react:

```python
from nova.db.brain import BrainDB

db = BrainDB("~/.nova/brain.db")

# Add a take (atomic knowledge claim)
db.add_take(
    claim="Sparse attention reduces quadratic complexity to O(n log n)",
    holder="my-research",
    kind="insight",
    weight=0.9,
)

# Check state
print(db.snapshot())
# {'takes': 19, 'orphan': 0, 'open_contra': 0, 'health': 97.5}
```

Or write a markdown file to `~/.nova/kb/`:

```bash
cat > ~/.nova/kb/my-insight.md << 'EOF'
---
title: My First Insight
type: concept
---

# My First Insight

Autonomous knowledge systems improve with every run.
EOF
```

The KB watcher detects the new file and syncs it to `brain.db` immediately.

---

## Step 7: Create your own harness

```bash
nova new my-pipeline --pattern pipeline
```

This scaffolds `harnesses/my-pipeline/harness.yaml`. Edit it:

```yaml
name: my-pipeline
pattern: pipeline

phases:
  - name: research
    executor: llm
    prompt: |
      Research the following topic and summarise key findings:
      Topic: {{context.topic}}
    output_file: research.md

  - name: write
    executor: llm
    prompt: |
      Using this research: {{research.md}}
      Write a concise technical blog post about {{context.topic}}.
    output_file: post.md
    quality_check:
      enabled: true
      threshold: 75
```

Run it:

```bash
nova run my-pipeline --context topic="vector databases in production"
```

---

## Complete CLI reference

```
nova setup                         Initialize ~/.nova data directory
nova watcher start                 Start brain + KB watchers (background)
nova watcher status                Show watcher state + brain.db stats
nova watcher stop                  Stop all watchers

nova run <harness>                 Run a harness end-to-end
nova run <harness> --resume        Resume from last checkpoint
nova run <harness> --dry-run       Dry run (no LLM calls, no writes)

nova new <name> --pattern pipeline Scaffold a new harness
nova list                          List available harnesses
nova evolution <harness>           Show run history and quality scores

nova kb search <query>             Search the knowledge base
nova kb list                       List all KB pages

nova inspect build                 Build architecture graph (current dir)
nova inspect report                Generate architecture report
nova inspect hotspots              Show most-connected nodes
nova inspect path --from A --to B  Find path between two code nodes
```

---

## What's next?

- [Writing Harnesses](writing-harnesses.md) — full harness YAML reference
- [Providers](providers.md) — LLM, publisher, notifier configuration
- [Quality Gates](quality-gates.md) — how automatic scoring works
- [Autonomous Event Loop](autonomous-event-loop.md) — deep dive into the watcher architecture
- [Custom Provider](custom-provider.md) — wire in your own LLM or publisher
