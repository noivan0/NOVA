# Quickstart Guide

This guide walks you through your first NOVA run in 5 minutes.

---

## Prerequisites

- Python 3.10 or higher
- An API key for at least one LLM provider (or use the built-in `echo` provider — no key needed)

---

## Step 1: Clone and install

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA
```

Choose your LLM provider:

```bash
# No API key? Use the echo provider — perfect for testing harness structure
pip install -e "."

# OpenAI (gpt-4o, o3, o4-mini, gpt-5 …)
pip install -e ".[openai]"

# Anthropic Claude (claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5)
pip install -e ".[anthropic]"

# Local Ollama — no API key, no cost
pip install -e ".[ollama]"

# All providers at once
pip install -e ".[all]"
```

Verify the installation:

```bash
nova --help
```

---

## Step 2: Try it immediately — no API key needed

NOVA includes an `echo` provider that mirrors your prompt back as output.
Use it to validate harness structure, test checkpointing, and explore the
CLI — all without touching an API.

```bash
# Set echo as your provider (no .env needed)
NOVA_LLM_PROVIDER=echo nova run research --context topic="AI agents"
```

You'll see the full pipeline execute: phases run, quality gates fire,
evolution log is written, output appears in `workspace/research/`.

---

## Step 3: Configure a real LLM

```bash
cp .env.example .env
```

Open `.env` and set your provider:

**OpenAI:**
```bash
NOVA_LLM_PROVIDER=openai
NOVA_LLM_MODEL=gpt-4o
NOVA_LLM_API_KEY=sk-...
```

**Anthropic Claude:**
```bash
NOVA_LLM_PROVIDER=anthropic
NOVA_LLM_MODEL=claude-sonnet-4-6
NOVA_LLM_API_KEY=sk-ant-...
```

**Ollama (local, free):**
```bash
# First: install Ollama from https://ollama.com and pull a model
# ollama pull llama3.3

NOVA_LLM_PROVIDER=ollama
NOVA_LLM_MODEL=llama3.3
# No API key needed
```

---

## Step 4: Dry run (preview without API calls)

Before spending any tokens, preview what will happen:

```bash
nova run research --context topic="artificial intelligence" --dry-run
```

You'll see the phase plan and prompt previews — no API calls made.

---

## Step 5: Run your first harness

### Research harness

Gathers information from multiple angles and synthesises a structured report:

```bash
nova run research --context topic="the future of AI agents"
```

Output appears in `workspace/research/`:
- `web_research.md` — research findings
- `kb_context.md` — relevant prior KB context
- `report.md` — synthesised final report

### Summarizer harness

Multi-level summary from any text (TL;DR, key points, deep analysis):

```bash
nova run summarizer --context text_file=my_article.txt
```

### Data Pipeline harness

Profiles a CSV file and produces an insight report:

```bash
nova run data-pipeline --context csv_file=data.csv
```

---

## Step 6: Inspect results

```bash
# See all available harnesses
nova list

# Check what ran
nova status research

# View run history and quality scores
nova evolution research

# Search your knowledge base
nova kb search "AI"
```

---

## Step 7: Resume an interrupted run

If a run is interrupted (network error, timeout, Ctrl+C), resume from exactly where it stopped:

```bash
nova run research --resume
```

NOVA reads the checkpoint and skips phases that already completed successfully.

---

## Step 8: Create your own harness

```bash
nova new my-workflow --pattern pipeline
```

This creates:
```
harnesses/my-workflow/
├── harness.yaml       ← edit this
└── prompts/
    └── step1.txt      ← edit your prompts
```

Edit `harness.yaml` and run:

```bash
nova run my-workflow --context key=value
```

---

## What's next?

- [Writing Harnesses](writing-harnesses.md) — full harness authoring guide
- [Providers](providers.md) — setup for all LLM, notifier, and publisher providers
- [Custom Provider](custom-provider.md) — add your own LLM or publisher backend
- [Quality Gates](quality-gates.md) — how automatic output scoring works
- [Architecture](../architecture.md) — deep-dive into how NOVA works internally
