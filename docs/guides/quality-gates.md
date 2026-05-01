# Understanding Quality Gates

NOVA's quality gate system automatically scores LLM phase output and
retries or blocks the harness if the score falls below a threshold.

---

## How It Works

### 1. Mark a phase as quality-checked

In your `harness.yaml`, set `quality_check: true` on any LLM phase:

```yaml
phases:
  - id: synthesis
    executor: llm
    prompt: "Summarize the research..."
    output_file: report.md
    quality_check: true      # <-- enables the gate
    on_failure: abort
```

### 2. NOVA sends a second LLM call (judge)

After the phase produces output, NOVA sends a separate scoring prompt to the
same LLM provider:

```
You are a quality evaluator. Score the following AI-generated output on a
scale from 0 to 100 based on:
  - Completeness (does it address the task?)
  - Accuracy (are claims internally consistent?)
  - Clarity (is it well-structured and readable?)
  - Relevance (does it stay on topic?)

Respond with ONLY a JSON object: {"score": <integer 0-100>, "reason": "<brief explanation>"}
```

The judge receives both the original prompt and the output, so it can
evaluate whether the output actually answered the question.

### 3. Compare against the threshold

The threshold is set in `nova.yaml`:

```yaml
quality_threshold: 70    # default
```

| Score | Outcome |
|-------|---------|
| >= threshold | Phase passes, harness continues |
| < threshold | Phase is retried (up to `max_retries` times) |
| Still < threshold after retries | `on_failure` rule applies |

### 4. Score is recorded in the evolution log

Every run records the final quality score in `evolution.jsonl`:

```json
{"run_id": "run_20260501_...", "quality_score": 84, "success": true, ...}
```

Use `nova evolution <harness>` to see quality trends over time.

---

## Configuring the Threshold

Set a global threshold in `nova.yaml`:

```yaml
quality_threshold: 70    # 0-100, default 70
```

The threshold applies to all quality-gated phases in all harnesses.
A higher threshold (e.g. 85) is appropriate for production pipelines;
a lower one (e.g. 50) is useful during development.

---

## Interpreting Scores

| Range | Meaning |
|-------|---------|
| 90–100 | Excellent — output is complete, accurate, and well-structured |
| 75–89 | Good — minor gaps or phrasing issues, but usable |
| 60–74 | Fair — some missing content or clarity issues |
| < 60 | Poor — significant problems; retry recommended |

Note: LLM scoring is not deterministic. Scores may vary by ±5 points
between runs. The evolution log helps you spot systematic issues.

---

## Disabling Quality Gates

To disable for a specific phase, simply omit `quality_check` (it defaults to `false`):

```yaml
phases:
  - id: draft
    executor: llm
    prompt: "..."
    # quality_check not set — no gate applied
```

To disable globally (e.g. during development), lower the threshold
or use `--dry-run` which skips all LLM calls entirely.

---

## Tips

- Put quality gates on the **final synthesis phase**, not every phase.
  Intermediate phases often produce partial output that would score low.
- If your gate is triggering too often, check the evolution log for
  patterns — it might be a prompt clarity issue, not an LLM issue.
- Use the `echo` provider (`NOVA_LLM_PROVIDER=echo`) for testing harness
  structure without consuming API quota.
