# Quickstart Guide

This guide walks you through your first NOVA run in 5 minutes.

---

## Prerequisites

- Python 3.10 or higher
- An API key for at least one LLM provider (or Ollama installed locally — free)

---

## Step 1: Clone and install

```bash
git clone https://github.com/noivan0/NOVA.git
cd NOVA
```

Choose your LLM provider:

```bash
# OpenAI (GPT-4o, o3, o4-mini …)
pip install -e ".[openai]"

# Anthropic Claude
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

## Step 2: Configure your LLM

```bash
cp .env.example .env
```

Open `.env` and set your provider. Minimal configuration:

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

## Step 3: Validate without API calls

Before spending any tokens, validate your setup with a dry run:

```bash
nova run research --context topic="artificial intelligence" --dry-run
```

You should see the phase plan and prompt previews. No API calls are made.

---

## Step 4: Run your first harness

### Research harness

Gathers information from multiple angles and synthesises a report:

```bash
nova run research --context topic="the future of AI agents"
```

Output appears in `workspace/research/`:
- `web_research.md` — research findings
- `kb_context.md` — relevant prior KB context
- `report.md` — synthesised final report

### Blog pipeline

Full pipeline: outline → draft → quality check → revise → publish:

```bash
nova run blog-pipeline \
  --context title="Why AI agents will change work" \
           keywords="AI,automation,productivity"
```

Output appears in `workspace/blog-pipeline/`:
- `outline.md`
- `draft.md`
- `quality_report.md`
- `final_post.md`
- `result.txt` — publish URL or local path

---

## Step 5: Inspect results

```bash
# See all harnesses
nova list

# Check what ran
nova status blog-pipeline

# View run history and quality scores
nova evolution blog-pipeline

# Search your knowledge base
nova kb search "AI"
```

---

## Step 6: Resume an interrupted run

If a run is interrupted (network error, timeout, Ctrl+C), resume from the exact phase that failed:

```bash
nova run blog-pipeline --resume
```

NOVA reads the checkpoint and skips phases that already completed successfully.

---

## Step 7: Create your own harness

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

Edit `harness.yaml` and `prompts/`, then run:

```bash
nova run my-workflow --context key=value
```

---

## What's next?

- [Writing Harnesses](writing-harnesses.md) — full harness authoring guide
- [Providers](providers.md) — setup for all LLM, notifier, and publisher providers
- [Custom Provider](custom-provider.md) — add your own LLM or publisher backend
- [Architecture](../architecture.md) — deep-dive into how NOVA works internally
