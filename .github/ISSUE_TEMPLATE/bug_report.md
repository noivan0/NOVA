---
name: Bug Report
about: Report something that isn't working correctly
title: '[Bug] '
labels: bug
assignees: ''
---

## Bug Description

A clear and concise description of what the bug is.

## Steps to Reproduce

```bash
# Minimal commands to reproduce
nova run <harness> --config nova.yaml
```

1. Step 1
2. Step 2
3. See error

## Expected Behavior

What you expected to happen.

## Actual Behavior

What actually happened. Include the full error output / stack trace:

```
paste error here
```

## Environment

| Item | Value |
|------|-------|
| NOVA version | `nova --version` output |
| Python version | `python --version` output |
| OS | e.g. Ubuntu 22.04 / macOS 14 / Windows 11 |
| LLM provider | openai / anthropic / ollama / custom |

## Configuration

Your `nova.yaml` (redact all API keys before pasting):

```yaml
paste nova.yaml here
```

## Additional Context

Any other context, screenshots, or logs that might help.
