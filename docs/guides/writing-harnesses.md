# Writing Harnesses

A harness is a YAML file that defines a complete AI workflow.
It lives in `harnesses/<name>/harness.yaml`.

---

## Minimal harness

```yaml
name: my-summary
description: "Summarise a topic"
version: "1.0.0"
pattern: pipeline

phases:
  - id: summarise
    name: "Summarise"
    executor: llm
    prompt: |
      Summarise "{{topic}}" in 3 clear bullet points.
    output_file: summary.md
    on_failure: retry
```

Run it:
```bash
nova run my-summary --context topic="quantum computing"
cat workspace/my-summary/summary.md
```

---

## Harness fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique harness identifier (matches directory name) |
| `description` | string | yes | What this harness does |
| `version` | string | yes | Semantic version |
| `pattern` | string | yes | `pipeline` \| `fanout` \| `supervisor` \| `generative` |
| `persona` | string | no | System prompt injected into every LLM call |
| `phases` | list | yes | Ordered list of phase definitions |
| `runbook` | list | no | Failure recovery rules |
| `evolution` | object | no | Run logging settings |

---

## Phase fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique phase identifier within harness |
| `name` | string | yes | Human-readable phase name |
| `description` | string | no | What this phase does |
| `executor` | string | yes | `llm` \| `shell` \| `python` \| `passthrough` |
| `prompt` | string | no | Inline prompt text (LLM executor) |
| `prompt_file` | string | no | Path relative to harness dir (LLM executor) |
| `command` | string | no | Shell command or Python code (shell/python executors) |
| `input_files` | list | no | Files from `workspace/` to inject into prompt |
| `output_file` | string | no | File to write output to in `workspace/` |
| `timeout` | int | no | Seconds (overrides global `phase_timeout`) |
| `retries` | int | no | Retry count (overrides global `max_retries`) |
| `quality_check` | bool | no | Enable quality gate scoring |
| `on_failure` | string | no | `retry` \| `skip` \| `abort` (default: `abort`) |

---

## Executors

### `llm` — Language model call

Sends a prompt to the configured LLM provider.

```yaml
- id: write
  executor: llm
  prompt_file: prompts/write.txt    # file relative to harness dir
  # OR inline:
  prompt: "Write a haiku about {{topic}}."
  input_files:
    - outline.md                    # injected as [outline.md]\n<content>
  output_file: draft.md
```

### `shell` — Shell command

Runs a command via `subprocess.run`. Output is captured and written to `output_file`.

```yaml
- id: process
  executor: shell
  command: "python scripts/process.py --input workspace/data.json"
  output_file: processed.txt
```

### `python` — Inline Python

Runs inline Python code. The following variables are injected into the execution scope:

| Variable | Type | Description |
|---|---|---|
| `context` | dict | All context variables from `--context` and orchestrator |
| `workspace` | `pathlib.Path` | Path to the harness workspace directory |
| `context["_publisher"]` | Publisher | Configured publisher instance |
| `context["_notifier"]` | Notifier | Configured notifier instance |
| `context["_kb"]` | KB | Knowledge base instance |
| `context["_phase_<id>"]` | str | Output of a previously completed phase |
| `output` | str (write this) | Set `output` to the string to save |

```yaml
- id: publish
  executor: python
  command: |
    publisher = context["_publisher"]
    content = (workspace / "final.md").read_text()
    url = publisher.publish(
        title=context["title"],
        content=content,
        tags=["ai", "automation"],
    )
    output = url or "not published"
  output_file: result.txt
```

### `passthrough` — Forward context

Does nothing — passes context unchanged to the next phase. Useful as a placeholder or
to create a named checkpoint in the evolution log.

```yaml
- id: checkpoint
  executor: passthrough
  output_file: status.txt
```

---

## Prompt files

Prompt files live in `harnesses/<name>/prompts/`. Use `{{variable}}` for substitution.

Available template variables:
- `{{key}}` — any `--context key=value` from the CLI
- `{{filename.md}}` — content of an `input_files` entry (filename only, not full path)
- `{{_phase_<id>}}` — text output of a completed phase

Example (`prompts/draft.txt`):
```
You are writing a blog post for {{audience}}.

Topic: {{title}}
Keywords to include: {{keywords}}

Based on this outline:
---
{{outline.md}}
---

Write a complete, engaging blog post (800-1200 words).
Structure: hook → problem → solution → examples → conclusion
```

---

## Quality gate

```yaml
- id: qa
  executor: llm
  prompt_file: prompts/qa.txt
  output_file: qa_report.md
  quality_check: true          # enable scoring
  on_failure: retry
```

The LLM must include a score in its response. Recognised formats:
```
SCORE: 85
Quality: 72/100
quality_score: 90
[SCORE=88]
85 out of 100
```

Example QA prompt (`prompts/qa.txt`):
```
Review the following content and score it from 0 to 100.

--- CONTENT ---
{{draft.md}}

Evaluate:
- Accuracy and depth (0-25)
- Clarity and structure (0-25)
- Engagement (0-25)
- Practical value (0-25)

Write specific notes for each criterion, then on the LAST LINE write:
SCORE: <number>/100
```

---

## RunBook (failure recovery)

```yaml
runbook:
  - symptom: "rate limit"       # matched case-insensitively in error message
    action: "wait:60"           # wait 60 seconds then retry

  - symptom: "timeout"
    action: "notify"            # send alert via configured notifier

  - symptom: "context length"
    action: "skip"              # skip this phase and continue

  - symptom: "connection error"
    action: "wait:30"
```

Built-in actions:
| Action | Behaviour |
|---|---|
| `wait:N` | Sleep N seconds then retry the phase |
| `notify` | Send a notifier alert with the error details |
| `skip` | Skip this phase, mark as skipped, continue |
| `abort` | Abort the entire run |

---

## Evolution log

```yaml
evolution:
  enabled: true
  file: evolution.md    # written to harnesses/<name>/evolution.md
```

Each run appends a Markdown entry with: run ID, status, duration, quality score, phases run, published URL.
A companion `evolution.jsonl` is maintained for programmatic analysis.

View with: `nova evolution <harness-name>`

---

## Execution patterns

### `pipeline`
Phases run sequentially. Each phase's output can be fed into the next.

### `fanout`
All phases run and their outputs are merged. Use for multi-angle research or A/B generation.

### `supervisor`
Like `pipeline` but halts immediately if quality threshold cannot be reached.

### `generative`
Like `pipeline` with higher temperature defaults and more tolerant failure handling.

---

## Full harness example

```yaml
name: research-and-write
description: "Research a topic and write a structured article"
version: "1.0.0"
pattern: pipeline

persona: |
  A knowledgeable technical writer who produces clear, well-researched articles
  for developers. You cite sources, explain concepts, and use concrete examples.

phases:
  - id: research
    name: "Research"
    executor: llm
    prompt_file: prompts/research.txt
    output_file: research.md
    timeout: 180
    on_failure: retry

  - id: outline
    name: "Outline"
    executor: llm
    input_files:
      - research.md
    prompt_file: prompts/outline.txt
    output_file: outline.md
    on_failure: retry

  - id: draft
    name: "Draft"
    executor: llm
    input_files:
      - research.md
      - outline.md
    prompt_file: prompts/draft.txt
    output_file: draft.md
    timeout: 300
    on_failure: retry

  - id: qa
    name: "Quality Check"
    executor: llm
    input_files:
      - draft.md
    prompt_file: prompts/qa.txt
    output_file: qa_report.md
    quality_check: true
    on_failure: retry

  - id: revise
    name: "Revise"
    executor: llm
    input_files:
      - draft.md
      - qa_report.md
    prompt_file: prompts/revise.txt
    output_file: final.md
    on_failure: abort

  - id: publish
    name: "Publish"
    executor: python
    command: |
      publisher = context["_publisher"]
      content = (workspace / "final.md").read_text()
      url = publisher.publish(title=context["title"], content=content)
      output = url or "saved locally"
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
