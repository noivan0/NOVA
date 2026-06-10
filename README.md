# NOVA — Autonomous AI Orchestration Framework

<p align="center">
  <b>Declarative workflows. Event-driven autonomy. Self-improving over time.</b><br/>
  Define complex AI pipelines in YAML. Run them reliably. Let NOVA improve itself.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"/>
  <img src="https://github.com/noivan0/NOVA/actions/workflows/ci.yml/badge.svg" alt="CI"/>
  <img src="https://img.shields.io/badge/tests-82%20passing-brightgreen?style=flat-square" alt="Tests"/>
  <img src="https://img.shields.io/badge/LLM-OpenAI%20%7C%20Anthropic%20%7C%20Ollama%20%7C%20Custom-orange?style=flat-square" alt="LLM Providers"/>
</p>

---

## What is NOVA?

NOVA is an AI orchestration framework built around two ideas:

**1. Declarative pipelines** — define multi-step AI workflows in a YAML harness file and run them with one command. Checkpointing, quality gates, failure recovery, and evolution logging are built in.

**2. Autonomous event loop** — instead of polling or cron jobs, NOVA watches its own knowledge database for real changes and reacts instantly. Knowledge accumulates → synthesis runs → wiki updates → next run is smarter. No timers. No wasted cycles.

---

## Installation

```bash
# Core (no API key required — works with the built-in echo provider)
pip install nova-orchestrator

# With OpenAI
pip install "nova-orchestrator[openai]"

# With Anthropic Claude
pip install "nova-orchestrator[anthropic]"

# With local Ollama (no cost, no API key)
pip install "nova-orchestrator[ollama]"

# Everything
pip install "nova-orchestrator[all]"
```

Verify:

```bash
nova --version
# nova 1.3.0
```

---

## Setup

Initialize your NOVA data directory. This creates `~/.nova/` with brain.db, engines, KB, wiki, and kanban directories — everything you need.

```bash
nova setup
```

What gets created:

```
~/.nova/
├── brain.db          ← SQLite knowledge store (pages, takes, events, health)
├── memory.md         ← Persistent agent memory (auto-trimmed at 85% capacity)
├── kb/               ← Knowledge base markdown files
│   ├── lessons/      ← Lesson pages (auto-indexed by wiki synthesizer)
│   └── synthesis/    ← Auto-generated synthesis pages
├── wiki/             ← Auto-generated wiki pages
│   ├── entities/     ← Entity pages + takes summary
│   └── concepts/     ← Concept pages + lessons index
├── kanban/boards/    ← Task tracking (optional, integrated with chain engine)
├── engines/          ← Built-in reaction engines (auto-installed)
│   ├── dream.py      ← DreamCycle: health score, consolidation (+100 takes / health<90)
│   ├── learn.py      ← Link takes to KB pages (+5 takes)
│   ├── synthesize.py ← Takes → KB synthesis pages (+15 takes)
│   ├── chain.py      ← Promote kanban tasks when dependencies complete
│   ├── fix_orphan.py ← Assign agents to unowned KB pages
│   └── memory_slim.py← Trim memory.md when >85% full
└── logs/             ← Watcher logs and pid files
```

Use a custom location:

```bash
nova setup --nova-home ~/my-project/nova

# Set in your shell so all nova commands use it
export NOVA_HOME=~/my-project/nova   # add to ~/.bashrc or ~/.zshrc
```

---

## Starting the Autonomous Watchers

The watchers replace cron jobs. They react to real changes in `brain.db` and `kb/` instantly — no polling.

**Linux only** — requires `inotify-tools`:

```bash
sudo apt-get install inotify-tools   # Debian/Ubuntu
sudo dnf install inotify-tools       # Fedora/RHEL
```

Start all watchers:

```bash
nova watcher start
```

Check status:

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

Stop:

```bash
nova watcher stop
```

> **macOS / Windows**: watchers require inotify (Linux). Harnesses still run and accumulate knowledge — trigger engines manually when needed: `python -m nova.engine.learn`, `python -m nova.engine.dream`, etc.

**What the watchers do:**

| Event | Engine triggered | Cooldown |
|---|---|---|
| takes +5 | `learn` — link takes to KB pages | 30 min |
| takes +15 | `synthesize` — write KB synthesis pages | 5 min |
| takes +100 | `dream` — health score + consolidation | 2 h |
| orphan ≥ 3 | `fix_orphan` — assign agents to pages | 30 s |
| health < 90 | `dream` — emergency consolidation | 2 h |
| kanban done++ | `chain` — promote dependent tasks | 10 s |
| memory ≥ 85% | `memory_slim` — trim memory.md | 30 min |

---

## Running a Harness

### No API key (echo provider — for testing)

```bash
nova run research --provider echo --context topic="transformer attention mechanisms"
```

### With OpenAI

```bash
export NOVA_LLM_API_KEY=sk-...
nova run research --context topic="transformer attention mechanisms"
```

### With Anthropic Claude

```bash
export NOVA_LLM_API_KEY=sk-ant-...
nova run research --provider anthropic --context topic="transformer attention mechanisms"
```

### With local Ollama

```bash
# Start Ollama first: ollama serve
nova run research --provider ollama --context topic="transformer attention mechanisms"
```

### Resume a failed run

```bash
nova run research --resume --context topic="transformer attention mechanisms"
```

Every run automatically accumulates knowledge takes into `brain.db`. The watchers pick these up and react — no manual steps needed.

---

## Creating Your Own Harness

```bash
nova new my-pipeline --pattern pipeline
```

Edit `harnesses/my-pipeline/harness.yaml`:

```yaml
name: my-pipeline
pattern: pipeline   # pipeline | fanout | supervisor | generative

phases:
  - name: research
    executor: llm
    prompt: |
      Research the following and summarise key findings:
      Topic: {{context.topic}}
    output_file: research.md

  - name: write
    executor: llm
    prompt: |
      Using this research:
      {{research.md}}

      Write a concise technical blog post about {{context.topic}}.
    output_file: post.md
    quality_check:
      enabled: true
      threshold: 75      # 0-100; auto-retries if below
      max_retries: 2

  - name: notify
    executor: shell
    command: "echo 'Done: {{output_dir}}/post.md'"
```

Run it:

```bash
nova run my-pipeline --context topic="vector databases in production"
```

---

## Adding Knowledge Directly

You can add knowledge directly — the watchers react automatically:

```python
from nova.db.brain import BrainDB

db = BrainDB("~/.nova/brain.db")

db.add_take(
    claim="Sparse attention reduces quadratic complexity to O(n log n)",
    holder="my-research",
    kind="insight",   # fact | insight | lesson | pattern
    weight=0.9,       # 0.0–1.0 quality score
)

# Check current state
print(db.snapshot())
# {'takes': 1, 'orphan': 0, 'open_contra': 0, 'health': 100.0}
```

Or drop a markdown file into `~/.nova/kb/` — the KB watcher syncs it to `brain.db` immediately:

```bash
cat > ~/.nova/kb/my-note.md << 'EOF'
---
title: My First Note
type: concept
---

# My First Note

Autonomous systems improve with every interaction.
EOF
```

---

## CLI Reference

```
nova --version                     Show version

nova setup                         Initialize ~/.nova data directory
nova setup --nova-home <path>      Use a custom data directory
nova setup --no-install-engines    Skip built-in engine installation

nova watcher start                 Start brain + KB watchers (background)
nova watcher start --no-hook       Skip hook server
nova watcher status                Show watcher state + brain.db stats
nova watcher stop                  Stop all watchers

nova run <harness>                 Run a harness end-to-end
nova run <harness> --resume        Resume from last checkpoint
nova run <harness> --dry-run       Dry run (no LLM calls, no writes)
nova run <harness> --provider <p>  Override LLM provider (openai|anthropic|ollama|echo)
nova run <harness> --context k=v   Pass context variables to the harness

nova new <name>                    Scaffold a new harness (pipeline pattern)
nova new <name> --pattern fanout   Scaffold with a specific pattern
nova list                          List available harnesses
nova evolution <harness>           Show run history and quality scores
nova status <harness>              Show checkpoint state

nova kb search <query>             Search the knowledge base
nova kb list                       List all KB pages
nova kb write <key> <file>         Write a file into the KB

nova inspect build                 Build architecture graph (current repo)
nova inspect report                Generate Markdown architecture report
nova inspect hotspots              Show most-connected nodes
nova inspect bridges               Show bridge nodes
nova inspect path --from A --to B  Find path between two nodes
```

---

## LLM Providers

Configure in `nova.yaml` or via environment variables:

| Provider | `nova.yaml` | Environment variable |
|---|---|---|
| OpenAI | `provider: openai` | `NOVA_LLM_API_KEY=sk-...` |
| Anthropic | `provider: anthropic` | `NOVA_LLM_API_KEY=sk-ant-...` |
| Ollama | `provider: ollama` | *(no key needed)* |
| Custom / Enterprise | `provider: custom`, `base_url: https://...` | `NOVA_LLM_API_KEY=...` |
| Echo (testing) | `provider: echo` | *(no key needed)* |

Example `nova.yaml`:

```yaml
llm:
  provider: openai
  model: gpt-4o
  api_key: ""          # leave blank, use NOVA_LLM_API_KEY env var
  max_tokens: 4096
  temperature: 0.7

notifier:
  backend: telegram
  token: ""            # NOVA_NOTIFIER_TOKEN

publisher:
  backend: local
  output_dir: ./output
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI  nova run <harness> [--resume] [--dry-run] --context k=v       │
└────────────────────────┬────────────────────────────────────────────┘
                         │
            ┌────────────▼────────────┐
            │      Config Layer        │
            │  nova.yaml + NOVA_* env  │
            └────────────┬────────────┘
                         │
            ┌────────────▼────────────┐
            │     Orchestrator         │
            │  load → resume → run     │
            │  → Evolution Log         │
            └──┬──────────┬───────────┘
               │          │
    ┌──────────▼──┐  ┌────▼──────────┐
    │   Harness   │  │   KB Layer    │
    │  phases[]   │  │  brain.db     │
    └──────┬──────┘  │  kb/ pages    │
           │         │  wiki/ synth  │
    ┌──────▼──────────────────────────────┐
    │           Phase Execution            │
    │  llm    → LLMProvider.complete()     │
    │  shell  → subprocess.run()           │
    │  python → exec(inline code)          │
    │                                      │
    │  Quality Gate: score < threshold     │
    │    → auto-retry (max_retries times)  │
    │  RunBook: failure → recover rule     │
    └─────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Autonomous Loop  (runs alongside harnesses, event-driven)          │
│                                                                      │
│  brain.db changes  →  BrainWatcher  →  learn / synthesize / dream   │
│  kb/ changes       →  KBWatcher     →  embed / wiki / index         │
│  POST /publish     →  HookServer    →  sync / geo_update            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Repository Structure

```
NOVA/
├── nova/
│   ├── core/        # Harness execution engine (orchestrator, checkpoint, evolution, config, kb)
│   ├── watcher/     # Autonomous event loop (brain.py, kb.py, hook_server.py)
│   ├── db/          # Brain database (BrainDB, SQLite DDL)
│   ├── engine/      # Built-in reaction engines (dream, learn, synthesize, chain, fix_orphan, memory_slim)
│   ├── wiki/        # Wiki synthesis (crosslink, stale, takes, lessons, index)
│   ├── kb/          # Agent KB Pattern (BM25+vector search, embedding sync)
│   ├── inspect/     # Architecture graph analysis
│   ├── providers/   # LLM / Publisher / Notifier adapters
│   └── cli/         # nova CLI (main.py)
├── harnesses/       # Built-in examples: research, summarizer, data-pipeline
├── examples/        # Python API examples + engine reference implementations
├── tests/           # 82 tests (unit + integration)
├── docs/
│   ├── architecture.md
│   └── guides/
│       ├── quickstart.md              # Step-by-step first run
│       ├── writing-harnesses.md       # Full harness YAML reference
│       ├── providers.md               # LLM/notifier/publisher setup
│       ├── quality-gates.md           # How quality scoring works
│       ├── custom-provider.md         # Add your own LLM or publisher
│       └── autonomous-event-loop.md   # Deep dive: event-driven autonomy
└── nova.yaml        # Default config (edit this)
```

---

## Development

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA
pip install -e ".[dev]"

# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_engines.py -v

# Run with coverage
pytest --cov=nova --cov-report=term-missing
```

Requirements: Python 3.10+. Core dependency: `pyyaml` only. All LLM SDKs are optional extras.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, coding style, and PR process.

Quick rules:
- All tests must pass (`pytest`)
- Add tests for new features
- No hardcoded paths or credentials
- Keep `nova.yaml` provider as `echo` (CI runs without an API key)

---

## License

MIT — see [LICENSE](LICENSE).
