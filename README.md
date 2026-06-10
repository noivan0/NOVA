# NOVA — Autonomous AI Orchestration Framework

<p align="center">
  <b>Declarative workflows. Event-driven autonomy. Self-improving over time.</b><br/>
  Define complex AI pipelines in YAML. Run them reliably. Let NOVA improve itself.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"/>
  <img src="https://github.com/noivan0/NOVA/actions/workflows/ci.yml/badge.svg" alt="CI"/>
  <img src="https://img.shields.io/badge/tests-37%20passing-brightgreen?style=flat-square" alt="Tests"/>
  <img src="https://img.shields.io/badge/LLM-OpenAI%20%7C%20Anthropic%20%7C%20Ollama%20%7C%20Custom-orange?style=flat-square" alt="LLM Providers"/>
</p>

---

## What is NOVA?

NOVA is an AI orchestration framework built around two core ideas:

**1. Declarative pipelines** — define multi-step AI workflows in a YAML harness file and run them with one command. Checkpointing, quality gates, failure recovery, and evolution logging are built in.

**2. Autonomous event loop** — instead of polling or cron jobs, NOVA watches its own knowledge database for real changes and reacts instantly. Learns accumulate → synthesize runs → wiki updates → next run is smarter. No timers. No wasted cycles.

```bash
# Run a research pipeline
nova run research --context topic="transformer attention mechanisms"

# Start the autonomous watchers
python -m nova.watcher.brain --nova-home ~/.nova
python -m nova.watcher.kb   --nova-home ~/.nova
```

---

## Quick Start

```bash
pip install nova-orchestrator

# 1. Initialize your NOVA data directory (creates brain.db, engines, kb, wiki, kanban)
nova setup

# 2. Start the autonomous watchers
nova watcher start

# 3. Check status
nova watcher status

# 4. Run your first harness
nova run research --context topic="transformer attention mechanisms"
```

That's it. NOVA is now watching your knowledge base. Every harness run accumulates
takes into `brain.db`. When enough takes accumulate, the watchers automatically trigger
learning, synthesis, and wiki updates — no cron jobs required.

### One-liner end-to-end test (no API key)

```bash
pip install nova-orchestrator
nova setup --nova-home /tmp/nova-test
nova run research --provider echo --nova-home /tmp/nova-test --context topic="test"
nova watcher status --nova-home /tmp/nova-test
```

---

## Key Features

### Declarative Workflows (Harness YAML)
- **4 execution patterns** — `pipeline` (sequential), `fanout` (parallel branches), `supervisor` (strict QA), `generative` (creative)
- **4 phase executors** — `llm` (LLM call), `shell` (subprocess), `python` (inline code), `passthrough`
- **Resumable** — checkpoint saved after every phase; `--resume` picks up exactly where it stopped
- **Quality Gates** — LLM self-scores its output; NOVA retries if below threshold
- **RunBook** — declarative failure recovery: `rate limit → wait 60s → retry`

### Autonomous Event Loop
- **Brain Watcher** — watches `brain.db` via inotify; reacts to new knowledge, health drops, orphaned pages
- **KB Watcher** — watches `kb/` and `skills/` for markdown changes; triggers embedding, index rebuild, wiki synthesis
- **Hook Server** — lightweight HTTP webhook receiver for publish-complete events
- **14 reaction types** — learn, synthesize, dream, chain, fix_orphan, memory_slim, wiki crosslink/takes/stale, and more
- **Zero cron jobs** — all reactions are event-driven, not timer-driven

### Knowledge Infrastructure
- **Brain DB** — SQLite-backed store for knowledge pages, atomic claims (takes), contradictions, and events
- **KB Pattern** — persistent markdown knowledge base with hybrid BM25 + vector search
- **Wiki Synthesis** — crosslink, stale refresh, takes summarization, lessons index
- **Evolution Log** — every harness run recorded in Markdown + JSONL; track quality trends across hundreds of runs

### Architecture Intelligence
- **Graph analysis** — build a structural graph of your codebase; find hotspots, bridges, critical paths
- **`nova inspect`** — CLI commands to query the graph without external tools

### Provider Abstraction
- **LLM** — OpenAI, Anthropic, Ollama, or any OpenAI-compatible endpoint
- **Publisher** — WordPress, Ghost, or local file
- **Notifier** — Telegram, Slack, Discord
- **Zero lock-in** — swap any provider via one config line

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
            │  LLM · Notifier · Pub    │
            └────────────┬────────────┘
                         │
            ┌────────────▼────────────┐
            │     Orchestrator         │
            │  load → resume → run     │
            │  → Evolution Log         │
            └──┬──────────┬───────────┘
               │          │
    ┌──────────▼─┐   ┌────▼──────────┐
    │  Harness   │   │   KB Layer    │
    │  YAML spec │   │  brain.db     │
    │  phases[]  │   │  kb/ pages   │
    └──────┬─────┘   │  wiki/ synth │
           │         └──────────────┘
    ┌──────▼────────────────────────────────────┐
    │              Phase Execution               │
    │  llm → LLMProvider.complete(prompt)        │
    │  shell → subprocess.run(command)           │
    │  python → exec(code)                       │
    │  passthrough → forward context             │
    │                                            │
    │  → Quality Gate (score < threshold → retry)│
    │  → RunBook (failure → recover rule)        │
    └───────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  Autonomous Loop (always running alongside harnesses)               │
│                                                                      │
│  brain.db changes → BrainWatcher → learn / synthesize / dream       │
│  kb/ changes     → KBWatcher    → kb_pipeline / wiki / index        │
│  POST /publish   → HookServer   → sync_published / geo_update       │
└────────────────────────────────────────────────────────────────────┘
```

---

## Directory Structure

```
nova-orchestrator/
├── nova/
│   ├── core/                # Harness engine
│   │   ├── orchestrator.py  # Main execution loop
│   │   ├── harness.py       # Harness YAML loader
│   │   ├── checkpoint.py    # Phase checkpointing
│   │   ├── evolution.py     # Run history log
│   │   ├── config.py        # Config + provider resolution
│   │   └── kb.py            # KB integration (inject prior context)
│   ├── watcher/             # Autonomous event loop
│   │   ├── brain.py         # BrainWatcher: brain.db inotify reactions
│   │   ├── kb.py            # KBWatcher: kb/ + skills/ inotify sync
│   │   └── hook_server.py   # HTTP webhook receiver for publish events
│   ├── db/                  # Brain database
│   │   ├── brain.py         # BrainDB: pages, takes, events
│   │   └── schema.py        # SQLite DDL
│   ├── wiki/                # Wiki synthesis
│   │   └── synthesize.py    # crosslink / takes / stale / index phases
│   ├── kb/                  # Agent KB Pattern module
│   │   ├── manager.py       # Read/write KB pages with frontmatter
│   │   ├── search.py        # Hybrid BM25 + cosine search
│   │   └── sync.py          # Incremental embedding sync (SQLite)
│   ├── inspect/             # Architecture graph analysis
│   │   ├── analyzer.py      # Build structural graph
│   │   ├── models.py        # Graph models
│   │   └── report.py        # Markdown report generator
│   ├── providers/           # LLM / Publisher / Notifier adapters
│   │   ├── llm.py
│   │   ├── publisher.py
│   │   └── notifier.py
│   └── cli/
│       └── main.py          # nova CLI entrypoint
├── harnesses/               # Built-in harness examples
│   ├── research/
│   ├── summarizer/
│   └── data-pipeline/
├── examples/                # Python API examples
├── tests/
│   ├── unit/
│   └── integration/
├── docs/
│   ├── architecture.md
│   └── guides/
│       ├── quickstart.md
│       ├── writing-harnesses.md
│       ├── providers.md
│       ├── quality-gates.md
│       ├── custom-provider.md
│       └── autonomous-event-loop.md
├── nova.yaml                # Default config
└── CHANGELOG.md
```

---

## Harness Example

`harnesses/research/harness.yaml`:

```yaml
name: research
pattern: pipeline
kb_namespace: research

phases:
  - name: web_search
    executor: llm
    prompt_file: prompts/web_search.txt
    output_file: search_results.md

  - name: synthesise
    executor: llm
    prompt_file: prompts/synthesis.txt
    input_files: [search_results.md]
    output_file: report.md
    quality_check:
      enabled: true
      threshold: 70
      max_retries: 2

  - name: notify
    executor: shell
    command: "echo 'Research complete: {{output_dir}}/report.md'"
```

Run it:

```bash
nova run research --context topic="quantum computing breakthroughs 2025"
```

---

## Autonomous Loop Quick Start

```bash
# Install inotify-tools (Linux)
sudo apt-get install inotify-tools

# Start brain watcher (reacts to brain.db changes)
python -m nova.watcher.brain --nova-home ~/.nova

# Start KB watcher (reacts to kb/ and skills/ changes)
python -m nova.watcher.kb --nova-home ~/.nova

# Optional: start hook server (receives publish-complete events)
python -m nova.watcher.hook_server --nova-home ~/.nova --port 9121
```

Use the `BrainDB` API to record knowledge takes:

```python
from nova.db.brain import BrainDB

db = BrainDB("~/.nova/brain.db")
db.init()

# Record a knowledge claim — triggers learn_engine when enough accumulate
db.add_take(
    claim="Hybrid BM25 + vector search outperforms pure vector on domain-specific queries",
    holder="nova-research",
    kind="insight",
    weight=0.9,
)
```

See [docs/guides/autonomous-event-loop.md](docs/guides/autonomous-event-loop.md) for the full guide.

---

## LLM Providers

| Provider | Config |
|---|---|
| OpenAI | `provider: openai`, `NOVA_LLM_API_KEY=sk-...` |
| Anthropic | `provider: anthropic`, `NOVA_LLM_API_KEY=sk-ant-...` |
| Ollama | `provider: ollama`, `base_url: http://localhost:11434/v1` |
| Custom / Enterprise | `provider: custom`, `base_url: https://your-gateway/v1` |
| Echo (no key, testing) | `provider: echo` |

---

## Knowledge Base (KB)

The `nova/kb/` module implements the [Agent KB Pattern](https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18) —
a persistent, compounding knowledge base that survives across harness runs.

```python
from nova.kb.search import KBSearch

search = KBSearch("~/.nova/kb", db_path="~/.nova/embeddings.db")

# Hybrid keyword + vector search
results = search.query("SSL certificate renewal", top_k=5)
for r in results:
    print(r["score"], r["path"], r["snippet"])
```

Features:
- Hybrid BM25 keyword + cosine vector search
- No external vector DB — runs entirely on SQLite
- Multi-namespace: search across KB + sessions + skills in one query
- Pluggable embedding backends: OpenAI, sentence-transformers, Ollama, or no-op
- Chunking by H2 sections for precise retrieval
- Incremental sync — only re-embeds changed files

---

## Development

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA
pip install -e ".[dev]"

# Run tests
pytest

# Run a specific test
pytest tests/unit/test_kb.py -v
```

Requirements: Python 3.10+, pyyaml (core only). LLM SDKs are optional extras.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide: setup, coding style, PR process.

Quick summary:
- Run `pytest` before submitting a PR
- Add tests for new features
- Keep `nova.yaml` provider set to `echo` (no API key required for CI)
- No hardcoded paths or credentials in any file

---

## License

MIT — see [LICENSE](LICENSE).
