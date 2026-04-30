# NOVA — Noivan Orchestrated Vanguard Architecture

> **A single-agent AI orchestration framework for autonomous, multi-phase project execution.**

NOVA lets you define complex AI workflows in a simple YAML file (a *harness*) and run them
end-to-end with a single command — with checkpointing, quality gates, self-improvement logs,
and pluggable LLM / publisher / notifier backends.

---

## Core Concepts

| Concept | What it is |
|---|---|
| **Harness** | YAML file defining a workflow — phases, prompts, executors, failure rules |
| **Phase** | One step: LLM call, shell command, or inline Python |
| **Checkpoint** | Auto-saved state — resume from the last completed phase if interrupted |
| **Quality Gate** | Parse a score from LLM output; retry if below threshold |
| **RunBook** | Automatic recovery rules for known failure modes (rate limits, timeouts, …) |
| **Evolution Log** | Per-harness run history in Markdown + JSONL — learn from every run |
| **KB** | Markdown knowledge base — persistent context across runs |
| **Provider** | Pluggable backends for LLM, notifications, and publishing |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  L0  Foundation — KB · Config · Provider abstractions        │
├─────────────────────────────────────────────────────────────┤
│  L1  Intake — CLI parses harness YAML + context variables    │
├─────────────────────────────────────────────────────────────┤
│  L2  Orchestrator — loads harness, restores checkpoint       │
├─────────────────────────────────────────────────────────────┤
│  L3  Execution — phases: llm | shell | python | passthrough  │
├─────────────────────────────────────────────────────────────┤
│  L4  Quality Gate — parse score → retry / pass / abort       │
├─────────────────────────────────────────────────────────────┤
│  L5  Evolution — log outcome, detect consecutive failures    │
├─────────────────────────────────────────────────────────────┤
│  L6  Observability — KB log + notifier alerts                │
└─────────────────────────────────────────────────────────────┘
```

**Single-agent design:** every phase runs in the same process.
No secondary agents, no IPC, no Docker orchestration required.

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA

# Install with your preferred LLM provider:
pip install -e ".[openai]"       # OpenAI GPT-4o / GPT-4.1 / o3 …
pip install -e ".[anthropic]"    # Anthropic Claude
pip install -e ".[ollama]"       # Local Ollama
pip install -e ".[all]"          # All providers
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set NOVA_LLM_API_KEY and NOVA_LLM_MODEL at minimum
```

### 3. Run a harness

```bash
# Research harness (fanout pattern — synthesises a topic from multiple angles)
nova run research --context topic="the future of AI agents"

# Blog pipeline (pipeline pattern — outline → draft → QA → revise → publish)
nova run blog-pipeline --context title="Why AI agents will change work" keywords="AI,automation"

# Resume an interrupted run (reads checkpoint from workspace/)
nova run blog-pipeline --resume

# Dry run — validate harness structure without any LLM calls
nova run research --context topic="test" --dry-run
```

### 4. Monitor

```bash
nova list                          # List available harnesses
nova status blog-pipeline          # Show current checkpoint
nova evolution blog-pipeline       # Show run history + failure rate
nova kb search "AI agents"         # Full-text search your KB
nova kb list                       # List all KB pages
```

### 5. Create a new harness

```bash
nova new my-workflow --pattern pipeline
# Fills in harnesses/my-workflow/harness.yaml with a working skeleton
# Edit the YAML and prompts/*, then run:
nova run my-workflow --context key=value
```

---

## Supported Providers

### LLM

| Provider | `llm.provider` | Recommended models | Install |
|---|---|---|---|
| **OpenAI** | `openai` | `gpt-4.1`, `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini` | `pip install "openai>=2.0"` |
| **Anthropic** | `anthropic` | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-6` | `pip install "anthropic>=0.97"` |
| **Ollama** (local) | `ollama` | `llama3.3`, `gemma3`, `qwen3`, `deepseek-r2`, `mistral` | `pip install "ollama>=0.6"` |
| **Custom endpoint** | `custom` | Any OpenAI-compatible API (LM Studio, vLLM, LocalAI, …) | — |
| **Echo** (testing) | `echo` | — Returns the prompt back, no API key needed | — |

> **Reasoning models (o1, o3, o4-mini):** NOVA automatically uses `max_completion_tokens`
> instead of `max_tokens` and omits `temperature` — these are set per the OpenAI API spec.

### Notifier

| Provider | `notifier.provider` | Notes |
|---|---|---|
| **None** | `none` | Default — silent |
| **Telegram** | `telegram` | Bot API (`token` + `chat_id`) |
| **Slack** | `slack` | Incoming Webhooks (`webhook_url`) |
| **Discord** | `discord` | Webhooks (`webhook_url`) |
| **Generic** | `webhook` | HTTP POST JSON to any endpoint |

### Publisher

| Provider | `publisher.provider` | Notes |
|---|---|---|
| **None** | `none` | Content stays in `workspace/` — default |
| **File** | `file` | Write to local filesystem (Hugo, Jekyll, …) |
| **WordPress** | `wordpress` | REST API v2 — Application Passwords (`user:app_password`) |
| **Ghost** | `ghost` | Admin API v5 — Admin API key (`id:hex_secret`) |
| **Blogger** | `blogger` | Google Blogger API v3 — OAuth2 access token |

---

## Harness YAML Reference

```yaml
name: my-harness
description: "What this workflow does"
version: "1.0.0"
pattern: pipeline          # pipeline | fanout | supervisor | generative

# System prompt injected into every LLM call in this harness (optional)
persona: |
  A senior technical writer producing clear, actionable documentation.

phases:
  - id: research
    name: "Research Phase"
    executor: llm            # llm | shell | python | passthrough
    prompt_file: research.txt
    output_file: research.md
    timeout: 180             # seconds (overrides global phase_timeout)
    retries: 2               # overrides global max_retries
    quality_check: true      # parse SCORE: N/100 from output; retry if < threshold
    on_failure: retry        # retry | skip | abort

  - id: write
    name: "Writing Phase"
    executor: llm
    input_files:
      - research.md          # injected into prompt from workspace/
    prompt_file: write.txt
    output_file: draft.md
    on_failure: abort

  - id: publish
    name: "Publish"
    executor: python
    command: |
      # _publisher is injected by the orchestrator — no nova imports needed
      publisher = context["_publisher"]
      content = (workspace / "draft.md").read_text()
      url = publisher.publish(title=context["title"], content=content)
      output = url or "not published"
    output_file: result.txt
    on_failure: skip

runbook:
  - symptom: "rate limit"
    action: "wait:60"
  - symptom: "timeout"
    action: "notify"

evolution:
  enabled: true
  file: evolution.md
```

### Patterns

| Pattern | Behaviour |
|---|---|
| `pipeline` | Phases run sequentially; each phase's output flows into the next |
| `fanout` | All phases run independently; results merged into `_fanout_results` |
| `supervisor` | Like pipeline, but with stricter quality enforcement |
| `generative` | Like pipeline, optimised for creative / generative workflows |

### Context variables

Pass `--context key=value` on the CLI. Inside prompts use `{{key}}`. Inside Python phases use `context["key"]`.

Special built-ins injected by the orchestrator:

| Key | Value |
|---|---|
| `_publisher` | Configured `Publisher` instance |
| `_phase_<id>` | Output of a completed phase |
| `_fanout_results` | Dict of all fanout phase outputs |
| `_last_quality_score` | Most recent parsed quality score |

### Quality gate

Add `quality_check: true` to a phase. The LLM output is scanned for a score in any of these formats:

```
SCORE: 85
Quality: 72/100
quality_score: 90
[SCORE=88]
85 out of 100
```

If the score is below `quality_threshold` (default 70), the phase retries. If no score is found the gate is skipped (not failed).

---

## Configuration

NOVA merges config from three layers (later layers override earlier ones):

1. Built-in defaults (see `nova/core/config.py`)
2. `nova.yaml` in the current directory
3. Environment variables (`NOVA_*`)

### `nova.yaml` example

```yaml
workspace: ./workspace
harnesses_dir: ./harnesses

llm:
  provider: openai
  model: gpt-4o
  max_tokens: 4096
  temperature: 0.7

notifier:
  provider: telegram
  token: ${NOVA_NOTIFIER_TOKEN}
  chat_id: ${NOVA_NOTIFIER_CHAT_ID}

publisher:
  provider: ghost
  base_url: https://myblog.ghost.io
  api_key: ${NOVA_PUBLISHER_API_KEY}

phase_timeout: 300
max_retries: 2
quality_threshold: 70
```

### Key environment variables

| Variable | Description |
|---|---|
| `NOVA_LLM_PROVIDER` | `openai` / `anthropic` / `ollama` / `custom` / `echo` |
| `NOVA_LLM_MODEL` | Model name (e.g. `gpt-4o`, `claude-sonnet-4-6`, `llama3.3`) |
| `NOVA_LLM_API_KEY` | API key for your LLM provider |
| `NOVA_LLM_BASE_URL` | Custom base URL for OpenAI-compatible endpoints |
| `NOVA_NOTIFIER_PROVIDER` | `none` / `telegram` / `slack` / `discord` / `webhook` |
| `NOVA_NOTIFIER_TOKEN` | Telegram bot token |
| `NOVA_NOTIFIER_CHAT_ID` | Telegram chat ID |
| `NOVA_NOTIFIER_WEBHOOK_URL` | Slack / Discord / generic webhook URL |
| `NOVA_PUBLISHER_PROVIDER` | `none` / `file` / `wordpress` / `ghost` / `blogger` |
| `NOVA_PUBLISHER_API_KEY` | Publisher API key / credentials |
| `NOVA_PUBLISHER_BASE_URL` | Publisher site URL |
| `NOVA_DRY_RUN` | `true` — skip all LLM calls, print prompts only |

---

## Project Structure

```
NOVA/
├── nova/
│   ├── core/
│   │   ├── config.py        # Configuration loading (YAML + env vars)
│   │   ├── harness.py       # Harness YAML parser and dataclasses
│   │   ├── orchestrator.py  # Execution engine — pipeline / fanout
│   │   ├── checkpoint.py    # Resumable state persistence
│   │   ├── evolution.py     # Run history (Markdown + JSONL)
│   │   └── kb.py            # Markdown knowledge base
│   ├── providers/
│   │   ├── llm.py           # OpenAI · Anthropic · Ollama · Custom · Echo
│   │   ├── notifier.py      # Telegram · Slack · Discord · Webhook
│   │   └── publisher.py     # WordPress · Ghost · Blogger · File
│   └── cli/
│       └── main.py          # `nova` CLI entrypoint
├── harnesses/
│   ├── blog-pipeline/       # Example: end-to-end blog post creation
│   └── research/            # Example: multi-angle research synthesis
├── tests/
│   ├── unit/                # Checkpoint, evolution, harness loader, KB
│   └── integration/         # Orchestrator with echo provider (no API key)
├── docs/
│   └── architecture.md      # Deep-dive architecture documentation
├── nova.yaml                # Default configuration (edit this)
├── .env.example             # Environment variable template
└── pyproject.toml           # Package metadata + optional deps
```

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
# All 14 tests pass with no API key needed (uses echo provider)
```

---

## Docker

```bash
docker compose run nova nova run research --context topic="AI agents"
```

---

## Contributing

1. Fork the repo
2. `pip install -e ".[dev]"`
3. Add a test for your change
4. `pytest tests/ -v && ruff check nova/ && black --check nova/`
5. Open a PR

---

## License

MIT — see [LICENSE](LICENSE)
