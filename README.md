# NOVA — Noivan Orchestrated Vanguard Architecture

> **A single-agent AI orchestration framework for autonomous project execution.**

NOVA lets you define complex, multi-phase AI workflows in a simple YAML file (a *harness*),
then run them end-to-end with a single command — with checkpointing, quality gates,
self-improvement logs, and pluggable LLM/publisher/notifier backends.

---

## Core Concepts

| Concept | What it is |
|---|---|
| **Harness** | A YAML file defining a workflow — phases, prompts, executors, failure rules |
| **Phase** | One step in a workflow (LLM call, shell command, or Python script) |
| **Checkpoint** | Auto-saved state — resume from where you left off if interrupted |
| **Quality Gate** | Score each phase output; retry if below threshold |
| **RunBook** | Automatic recovery rules for known failure modes |
| **Evolution Log** | Per-harness run history — learn from successes and failures |
| **KB** | Markdown knowledge base — persistent context across runs |
| **Provider** | Pluggable backend for LLM, notifications, and publishing |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  L0  Foundation — KB, config, providers                  │
├─────────────────────────────────────────────────────────┤
│  L1  Intake — CLI parses harness + context               │
├─────────────────────────────────────────────────────────┤
│  L2  Orchestrator — loads harness, restores checkpoint   │
├─────────────────────────────────────────────────────────┤
│  L3  Execution — phases run: llm | shell | python        │
├─────────────────────────────────────────────────────────┤
│  L4  Quality Gate — score → retry / pass / abort        │
├─────────────────────────────────────────────────────────┤
│  L5  Evolution — log outcome, detect failure patterns   │
├─────────────────────────────────────────────────────────┤
│  L6  Observability — KB log + notifier alert            │
└─────────────────────────────────────────────────────────┘
```

**Single-agent design:** every phase runs in the same process.
No secondary agents, no IPC, no SSH required.

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA
pip install -e ".[openai]"      # or [anthropic] for Claude
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set NOVA_LLM_API_KEY and NOVA_LLM_MODEL at minimum
```

### 3. Run a harness

```bash
# Research harness — synthesizes a topic from multiple angles
nova run research --context topic="the future of AI agents"

# Blog pipeline — outline → draft → quality check → revise → publish
nova run blog-pipeline --context title="Why AI agents will change work" keywords="AI,automation"

# Resume an interrupted run
nova run blog-pipeline --resume

# Dry run (no LLM calls — for testing harness structure)
nova run research --context topic="test" --dry-run
```

### 4. Monitor

```bash
nova list                        # List available harnesses
nova status blog-pipeline        # Check checkpoint
nova evolution blog-pipeline     # Show run history
nova kb search "AI agents"       # Search your KB
```

### 5. Create a new harness

```bash
nova new my-workflow --pattern pipeline
# Edit harnesses/my-workflow/harness.yaml
# Edit harnesses/my-workflow/prompts/*.txt
nova run my-workflow --context key=value
```

---

## Supported Providers

### LLM

| Provider | Config value | Notes |
|---|---|---|
| OpenAI | `openai` | GPT-4o, GPT-4, etc. |
| Anthropic | `anthropic` | Claude 3.5 Sonnet, etc. |
| Ollama | `ollama` | Local inference, no API key |
| Custom endpoint | `custom` | Any OpenAI-compatible API |
| Echo (testing) | `echo` | Returns prompt back — no API needed |

### Notifier

| Provider | Config value | Notes |
|---|---|---|
| None (silent) | `none` | Default — no notifications |
| Telegram | `telegram` | Bot API |
| Slack | `slack` | Incoming Webhooks |
| Discord | `discord` | Webhooks |
| Generic | `webhook` | HTTP POST JSON |

### Publisher

| Provider | Config value | Notes |
|---|---|---|
| None | `none` | Output stays in workspace/ |
| File | `file` | Write to local directory (static sites) |
| WordPress | `wordpress` | REST API with App Password |
| Ghost | `ghost` | Admin API |
| Blogger | `blogger` | Google Blogger API v3 |

---

## Harness YAML Reference

```yaml
name: my-harness
description: "What this harness does"
version: "1.0.0"
pattern: pipeline        # pipeline | fanout | supervisor | generative

# Optional: describe your target user
persona: |
  A professional content creator targeting intermediate developers.

phases:
  - id: research          # Unique phase ID
    name: "Research"      # Human-readable name
    executor: llm         # llm | shell | python | passthrough
    prompt_file: research.txt   # Load prompt from prompts/
    output_file: research.md    # Save output to workspace/
    input_files:          # Read from workspace/ (injected into prompt)
      - previous.md
    quality_check: true   # Enable quality gate for this phase
    timeout: 120          # Override global timeout (seconds)
    retries: 2            # Override global max_retries
    on_failure: retry     # retry | skip | abort | runbook

  - id: write
    executor: shell
    command: "python scripts/process.py --input research.md"
    output_file: result.md
    on_failure: abort

# Automatic recovery rules
runbook:
  - symptom: "rate limit"       # Matched against error message
    action: "wait:60"           # wait:N | notify | any shell command
    escalate_after: 3600        # Seconds before notifying

evolution:
  enabled: true
  file: evolution.md
```

---

## Directory Structure

```
NOVA/
├── nova/
│   ├── core/
│   │   ├── config.py         # Config loading (YAML + env vars)
│   │   ├── harness.py        # Harness definition and loader
│   │   ├── orchestrator.py   # Main execution engine
│   │   ├── checkpoint.py     # Resumable state management
│   │   ├── evolution.py      # Run history and self-improvement
│   │   └── kb.py             # Knowledge base (markdown-based)
│   ├── providers/
│   │   ├── llm.py            # LLM provider abstraction
│   │   ├── notifier.py       # Notification provider abstraction
│   │   └── publisher.py      # Content publisher abstraction
│   └── cli/
│       └── main.py           # CLI entry point
├── harnesses/
│   ├── research/             # Example: fanout research harness
│   │   ├── harness.yaml
│   │   └── prompts/
│   └── blog-pipeline/        # Example: pipeline blog harness
│       ├── harness.yaml
│       └── prompts/
├── docs/
│   ├── architecture.md       # Deep-dive architecture docs
│   └── guides/               # How-to guides
├── tests/
├── nova.yaml                 # Default config (copy and customize)
├── .env.example              # Environment variable template
├── pyproject.toml
└── docker-compose.yml
```

---

## Execution Patterns

| Pattern | Use when | How phases run |
|---|---|---|
| `pipeline` | Steps depend on each other | Sequential, output → next input |
| `fanout` | Independent parallel research | Sequential in single-agent mode, results merged |
| `supervisor` | Dynamic routing needed | Orchestrator decides per-step |
| `generative` | Iterative refinement | Retry loop until quality threshold met |

---

## Docker

```bash
cp .env.example .env
# Edit .env

# Run a harness
NOVA_HARNESS=research NOVA_TOPIC="quantum computing" docker compose up

# Interactive CLI
docker compose run nova-cli run research --context topic="my topic"
docker compose run nova-cli list
docker compose run nova-cli evolution research
```

---

## Knowledge Base

NOVA maintains a local KB in `./kb/` — a set of markdown files that persist across runs.

```bash
nova kb search "travel content"     # Find relevant past notes
nova kb write projects/my-harness notes.md   # Store notes
nova kb list                         # See all KB pages
```

The KB is automatically updated after every harness run with:
- Run status and duration
- Quality scores per phase
- Failure causes and RunBook activations

---

## Contributing

1. Fork the repo
2. Create a harness for your use case: `nova new my-use-case`
3. Add it to `harnesses/` with example prompts
4. Open a PR

See [docs/architecture.md](docs/architecture.md) for the full design spec.

---

## License

MIT — see [LICENSE](LICENSE)
