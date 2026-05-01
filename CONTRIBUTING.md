# Contributing to NOVA

Thank you for your interest in contributing to NOVA!
This document explains how to set up your environment, run tests,
and submit high-quality contributions.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Development Setup](#development-setup)
3. [Project Structure](#project-structure)
4. [Running Tests](#running-tests)
5. [Code Style](#code-style)
6. [How to Contribute](#how-to-contribute)
7. [Pull Request Process](#pull-request-process)
8. [Reporting Bugs](#reporting-bugs)
9. [Requesting Features](#requesting-features)

---

## Getting Started

Before contributing, please:

1. Read the [README](README.md) and [Quickstart](docs/guides/quickstart.md)
2. Check [open issues](https://github.com/noivan0/NOVA/issues) to avoid duplicate work
3. For large changes, open an issue first to discuss the approach

---

## Development Setup

```bash
# Clone the repository
git clone https://github.com/noivan0/NOVA.git
cd NOVA

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install in editable mode with all dev dependencies
pip install -e ".[dev]"

# Verify the setup
nova --help
python -m pytest tests/ -v
```

---

## Project Structure

```
nova/
  cli/          # CLI entry point (nova run, nova list, ...)
  core/         # Core engine
    config.py       # Config loading (nova.yaml + env vars)
    harness.py      # Harness YAML parser and dataclasses
    orchestrator.py # Execution engine (pipeline / fanout)
    checkpoint.py   # Resumable run state
    evolution.py    # Per-run history logging
    kb.py           # Local knowledge base
  providers/    # Pluggable backends
    llm.py          # OpenAI / Anthropic / Ollama / custom / echo
    notifier.py     # Telegram / Slack / Discord / webhook / none
    publisher.py    # file / WordPress / Ghost / none

harnesses/      # Built-in harness examples
  research/         # Multi-angle research report
  summarizer/       # Long-form content summarizer
  data-pipeline/    # CSV data analysis pipeline

docs/           # Documentation
  guides/
    quickstart.md
    providers.md
    custom-provider.md
    writing-harnesses.md
    quality-gates.md

tests/
  unit/         # Unit tests (no LLM calls, no network)
  integration/  # Integration tests (echo provider only)
```

---

## Running Tests

```bash
# Run all tests (uses echo provider — no API key required)
python -m pytest tests/ -v

# Run only unit tests
python -m pytest tests/unit/ -v

# Run with coverage report
python -m pytest tests/ --cov=nova --cov-report=term-missing

# Run a specific test file
python -m pytest tests/unit/test_harness_loader.py -v
```

All tests use the `echo` provider and require **no API key**.
Never add tests that make real LLM API calls — use `echo` or mock the provider.

---

## Code Style

NOVA uses standard Python tooling:

```bash
# Format code
black nova/ tests/

# Lint
ruff check nova/ tests/

# Type check
mypy nova/
```

These checks run automatically in CI. All three must pass before a PR is merged.

Guidelines:
- Line length: 100 characters
- Type annotations on all public functions
- Docstrings on all public classes and non-trivial functions
- No external dependencies unless strictly necessary (core must stay minimal)

---

## How to Contribute

### Adding a New LLM Provider

1. Add a class to `nova/providers/llm.py` inheriting from `LLMProvider`
2. Implement `complete(prompt, system, timeout) -> str`
3. Register it in the `get_llm_provider()` factory function
4. Add a test in `tests/unit/` that mocks the HTTP call
5. Document it in `docs/guides/providers.md`

### Adding a New Notifier

Same pattern as LLM — inherit `Notifier` in `nova/providers/notifier.py`,
implement `send(message) -> bool`, register in factory, add test and docs.

### Adding a New Publisher

Same pattern — inherit `Publisher` in `nova/providers/publisher.py`,
implement `publish(title, content, tags, metadata) -> Optional[str]`.

### Adding a New Harness Example

1. Create `harnesses/<name>/harness.yaml`
2. Follow the existing examples (research, summarizer, data-pipeline)
3. Use only the `echo` provider in any automated test for it
4. Mention it in the README harness table

### Adding Documentation

- Docs live in `docs/guides/`
- Follow the existing style: short intro, code blocks, no jargon
- Update `docs/` index links if you add a new page

---

## Pull Request Process

1. **Fork** the repository and create a branch from `main`
   ```
   git checkout -b feature/my-feature
   ```

2. **Make your changes** — keep each PR focused on one thing

3. **Add tests** — every new feature or bug fix needs a test

4. **Check locally before pushing:**
   ```bash
   black nova/ tests/
   ruff check nova/ tests/
   mypy nova/
   python -m pytest tests/ -v
   ```

5. **Write a clear commit message:**
   ```
   feat: add Discord notifier provider
   fix: checkpoint resume skips wrong phase index
   docs: add quality gate explanation
   ```
   We follow [Conventional Commits](https://www.conventionalcommits.org/).

6. **Open the PR** — fill in the PR template, link any related issues

7. **CI must pass** — all checks run automatically on push

8. **One reviewer approval** required before merge

---

## Reporting Bugs

Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md).

Please include:
- NOVA version (`nova --version`)
- Python version
- OS
- Minimal reproduction steps
- Full error output (stack trace)
- Your `nova.yaml` (redact API keys)

---

## Requesting Features

Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md).

Please describe:
- The problem you're trying to solve
- Your proposed solution
- Alternatives you considered

---

## Questions?

Open a [Discussion](https://github.com/noivan0/NOVA/discussions) on GitHub.
We aim to respond within 48 hours.
