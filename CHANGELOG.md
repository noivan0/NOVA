# Changelog

All notable changes to NOVA are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).
NOVA uses [Semantic Versioning](https://semver.org/).

---

## [1.2.0] — 2026-05-12

### Added
- `nova/kb/` — Agent KB Pattern module: persistent, compounding knowledge base for autonomous agents
  - `KBManager` — read/write KB pages with YAML frontmatter validation
  - `KBSync` — incremental embedding sync into SQLite (hash-based, no redundant re-embeds)
  - `KBSearch` — hybrid BM25 keyword + cosine vector search, no external vector DB required
  - Pluggable embedding backends: OpenAI, sentence-transformers, Ollama, or no-op (keyword only)
  - Multi-namespace search (main KB + project KB + sessions in one query)
  - Chunking by H2 sections for precise retrieval
- `examples/kb_quickstart.py` — self-contained example (runs with no API key)
- Pattern canonical doc: [Agent KB Pattern Gist](https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18)

## [1.1.0] — 2026-05-01

### Added
- `summarizer` harness — multi-level content summarizer (TL;DR, key points, deep analysis)
- `data-pipeline` harness — CSV profiling, LLM insight extraction, and report generation
- `CONTRIBUTING.md` — full contributor guide (setup, style, PR process)
- `CHANGELOG.md` — this file
- GitHub Actions CI — automated tests on Python 3.10 / 3.11 / 3.12
- GitHub Issue templates (Bug Report, Feature Request)
- GitHub Pull Request template
- `docs/guides/quality-gates.md` — explains how quality scoring works
- Echo provider now documented in Quickstart for zero-API-key testing

### Changed
- README restructured: cleaner harness table, echo provider highlighted in setup section
- `quickstart.md` now shows `echo` provider as the first option (no API key required)

### Removed
- All references to Blogger publishing backend (simplification)
- Internal `blog-pipeline` harness (was not general-purpose)

---

## [1.0.0] — 2026-04-25

### Added
- Core orchestration engine (`Orchestrator`) with `pipeline` and `fanout` execution patterns
- Harness YAML format — declarative multi-phase AI workflows
- 4 phase executors: `llm`, `shell`, `python`, `passthrough`
- Quality gates — LLM-as-judge auto-scoring with configurable threshold
- Checkpointing — resume harness runs from the last completed phase
- Evolution log — per-run history in `evolution.md` + `evolution.jsonl`
- Knowledge base (`KB`) — local markdown KB with append-log
- Runbook — auto-recovery rules triggered on phase failure
- LLM providers: `openai`, `anthropic`, `ollama`, `custom`, `echo`
- Notifier providers: `none`, `telegram`, `slack`, `discord`, `webhook`
- Publisher providers: `none`, `file`, `wordpress`, `ghost`
- CLI: `nova run`, `nova list`, `nova status`, `nova evolution`, `nova kb`, `nova new`
- `research` harness — multi-angle research report (fanout pattern)
- Docker support (`docker-compose.yml`)
- 14 unit and integration tests (all using `echo` provider)
- Full documentation: quickstart, providers guide, custom provider guide, harness writing guide

---

[1.1.0]: https://github.com/noivan0/NOVA/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/noivan0/NOVA/releases/tag/v1.0.0
