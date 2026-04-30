# NOVA Architecture — Deep Dive

<p align="center">
  <img src="../docs/nova-architecture-detail.svg" alt="NOVA Architecture Detail" width="860"/>
</p>

## Design Principles

1. **Single-agent first** — works with one agent, one LLM provider, one process
2. **Declarative workflows** — all logic in harness.yaml, not buried in code
3. **Resumable by default** — checkpoint after every phase; restart safely
4. **Provider-agnostic** — swap LLM, notifier, publisher via config/env vars
5. **Self-improving** — every run is recorded; failure patterns trigger alerts
6. **KB-driven context** — persistent knowledge base survives across sessions

---

## Execution Flow

```
nova run my-harness --context key=value
          │
          ▼
    load_config()          ← nova.yaml + env vars
          │
          ▼
    HarnessLoader.load()   ← harnesses/my-harness/harness.yaml
          │
          ▼
    Checkpoint.resume()    ← restore state if interrupted
          │
          ▼
    Orchestrator.run()
          │
          ├─ pattern=pipeline  → _run_pipeline()
          │     │
          │     └─ for each phase:
          │           │
          │           ├─ executor=llm    → LLMProvider.complete(prompt)
          │           ├─ executor=shell  → subprocess.run(command)
          │           └─ executor=python → exec(code)
          │                 │
          │                 ├─ success → write output_file → advance
          │                 ├─ quality_check → score < threshold → retry
          │                 └─ failure → RunBook → skip/retry/abort
          │
          └─ pattern=fanout → _run_fanout() → sequential, results merged
                │
                ▼
    Checkpoint.complete()   ← remove checkpoint on success
          │
          ▼
    EvolutionLog.record()   ← append to evolution.md + evolution.jsonl
          │
          ▼
    KB.append_log()         ← one-line entry in kb/log.md
          │
          ▼
    Notifier.send()         ← alert on consecutive failures
```

---

## Provider Abstraction

All three provider types follow the same pattern:
1. Abstract base class defines the interface
2. Concrete implementations for each backend
3. Factory function `get_xxx_provider(cfg)` instantiates the right one
4. Config comes from `nova.yaml` or `NOVA_*` env vars

```python
# Swap providers without changing harness code
from nova.providers.llm import get_llm_provider
llm = get_llm_provider(config.llm)
response = llm.complete("Your prompt here")
```

---

## Harness Patterns

### Pipeline (default)
Each phase feeds its output to the next. Output files from phase N are
available as input_files in phase N+1.

```
phase_1 → output_1.md → phase_2 → output_2.md → phase_3
```

### Fanout
All phases run independently (sequentially in single-agent mode).
Results are merged into `context['_fanout_results']` for a final synthesis phase.

```
phase_1 ─┐
phase_2 ─┼→ context['_fanout_results'] → synthesis_phase
phase_3 ─┘
```

### Supervisor (advanced pipeline)
Same as pipeline but phases can dynamically skip or branch based on
context state. Implement branching via `executor=python` phases.

### Generative (quality loop)
A phase with `quality_check=true` and `retries>0` will retry until the
quality score exceeds `quality_threshold`. Useful for content generation
where quality matters more than speed.

---

## Checkpoint Schema

```json
{
  "harness": "research",
  "state": { "topic": "Paris", "format": "long-form" },
  "started_at": "2026-04-25T10:00:00Z",
  "phase_started_at": "2026-04-25T10:03:22Z",
  "stale_threshold_secs": 300
}
```

If a checkpoint is older than `stale_threshold_secs`, it's automatically
cleared and the run restarts from the beginning.

---

## Evolution Log Schema

```json
{
  "run_id": "run_20260425_100000_abc123",
  "harness": "research",
  "pattern": "pipeline",
  "started_at": "2026-04-25T10:00:00Z",
  "finished_at": "2026-04-25T10:04:32Z",
  "duration_secs": 272.4,
  "success": true,
  "quality_score": 82,
  "phases_run": ["web_search", "synthesis"],
  "phases_failed": [],
  "runbook_fired": [],
  "improvements": [],
  "notes": ""
}
```

After 3+ consecutive failures, NOVA automatically sends a notification.
After 10+ runs, `failure_rate()` gives you a trend to act on.

---

## KB Structure

```
kb/
  index.md          ← auto-updated table of contents
  log.md            ← append-only activity log (one line per run)
  config/           ← system configuration notes
  fixes/            ← documented fixes and workarounds
  projects/         ← per-project notes and context
  user/             ← user preferences and domain knowledge
```

Use the KB as a persistent context store across harness runs:

```python
# In a python executor phase:
from nova.core.kb import KB
kb = KB("./kb")

# Store something from this run
kb.write("projects/my-harness/last-run", f"Topic: {context['topic']}\nScore: 82")

# Read prior context in the next run
prior = kb.read("projects/my-harness/last-run") or ""
```

---

## KB Bidirectional Flow

The KB feeds prompts and receives results — this is the core of NOVA's self-improvement loop.

```
              ┌────────────────────────────┐
              │         harness.yaml       │
              │  prompt: |                 │
              │    {{kb:projects/topic}}   │  ← KB injected into every prompt
              └────────────┬───────────────┘
                           │ KB.read() at phase start
                           ▼
              ┌────────────────────────────┐
              │      LLM Phase runs        │
              │      produces output       │
              └────────────┬───────────────┘
                           │ after phase / run completion
                           ▼
              ┌────────────────────────────┐
              │  KB.append_log()           │  ← one-line entry in kb/log.md
              │  KB.write("projects/…")    │  ← store analysis, notes, errors
              └────────────────────────────┘
```

**Growing context loop:** Run 1 writes basic notes. Run 2 reads those notes before prompting the LLM — producing richer output. The harness improves automatically without any code change.

**Prompt injection syntax:** Use `{{kb:key}}` in prompt files or harness.yaml prompts:

```yaml
phases:
  - id: research
    executor: llm
    prompt: |
      Use the following prior research when writing this report:
      {{kb:projects/my-topic-research}}

      Now research: {{topic}}
```

**Search across the KB:** Use `kb.search("keyword")` to find relevant pages before deciding what to inject.

---

## Context Lifecycle

`context` is a plain Python `dict` that flows through every phase of a run.

```
nova run research --context topic="AI agents" format="long-form"
          │
          ▼
ctx = {
  "topic": "AI agents",          ← from --context flags
  "format": "long-form",
  "_publisher": <Publisher>,     ← injected by Orchestrator at startup
  "_kb": <KB>,                   ← available to python phases
  "_notifier": <Notifier>        ← available to python phases
}
          │
          ▼  Phase 1 (web_search) completes
ctx["_phase_web_search"] = "<full LLM output>"   ← available to all later phases
          │
          ▼  Phase 2 (synthesis) prompt uses: {{_phase_web_search}}
ctx["_last_quality_score"] = 88                  ← tracked for evolution log
          │
          ▼  Run complete
  context is discarded — persisted data lives in workspace/ and kb/
```

**Serialization rule:** Only primitive values (`str`, `int`, `float`, `bool`, `list`, `dict`, `None`) are serialized to `checkpoint.json`. Keys starting with `_` (like `_publisher`) are excluded and rebuilt fresh on resume.

---

## Checkpoint Lifecycle

```
nova run …
    │
    ├─ checkpoint.exists() ?
    │      │
    │      ├─ YES → checkpoint.resume()
    │      │           │
    │      │           ├─ stale? (phase_started_at + stale_threshold_secs < now)
    │      │           │     └─ YES → checkpoint.complete() → restart from phase 0
    │      │           │
    │      │           └─ fresh? → skip phases 0..N-1 → resume from phase N
    │      │
    │      └─ NO  → checkpoint.start()  → new run_id, phase=0
    │
    ├─ [per phase] checkpoint.update(phase_index, phase_id, serializable_ctx)
    │
    └─ success → checkpoint.complete()  → checkpoint.json deleted
```

`stale_threshold_secs` defaults to `phase_timeout` from config (default: 300s).
If a phase hangs or the process is killed, the next run detects the stale checkpoint and restarts cleanly.

---

## Extending NOVA

### Adding a new LLM provider

```python
# nova/providers/llm.py
class MyProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        self.client = MySDK(api_key=cfg.api_key)
        self.model = cfg.model

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        return self.client.generate(prompt, model=self.model)

# In get_llm_provider():
elif p == "myprovider":
    return MyProvider(cfg)
```

### Adding a new publisher

```python
# nova/providers/publisher.py
class MyPublisher(Publisher):
    def publish(self, title, content, tags=None, metadata=None) -> Optional[str]:
        # Your publishing logic
        return "https://mysite.com/posts/slug"

# In get_publisher():
elif p == "mypublisher":
    return MyPublisher(cfg)
```

### Custom phase executor

For complex logic not covered by `llm`, `shell`, or `python`:
use `executor: python` and put your logic inline in `command`.

```yaml
phases:
  - id: fetch_data
    executor: python
    command: |
      import json, urllib.request
      url = f"https://api.example.com/data?q={context['topic']}"
      with urllib.request.urlopen(url) as r:
          data = json.loads(r.read())
      output = json.dumps(data['results'][:5], indent=2)
    output_file: data.json
```
