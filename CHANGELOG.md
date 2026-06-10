# Changelog

All notable changes to NOVA are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).
NOVA uses [Semantic Versioning](https://semver.org/).

---

## [1.3.0] — 2026-06-10

### Added
- **Autonomous Event Loop** — inotify-based brain watcher and KB watcher replace all cron jobs
  - `brain_watcher` reacts to `nova_brain.db` / `kanban.db` changes instantly (non-recursive, noise-free)
  - `kb_watcher` reacts to KB markdown and `SKILL.md` changes (recursive, with debounce)
  - 14 cron jobs eliminated: dream, brain-sync, skill-kb-bridge, wiki-synthesize, kb-index, memory-slim, resource collectors, and more
- **Cascade (piggyback) reactions** on brain events:
  - `wiki crosslink` after synthesize/dream (6 h cooldown)
  - `wiki takes summary` after dream (12 h cooldown)
  - `wiki stale refresh` after dream (24 h cooldown, background)
  - `RSS resource update` after synthesize/dream/learn (6 h cooldown)
  - `resource collector` (deep) after dream (7-day cooldown, background)
- **Published hook server v2** — POST /publish now triggers `sync_published` (10 min cooldown)
  and `geo_bible update` (6 h cooldown, only when data changed) immediately
- `docs/guides/autonomous-event-loop.md` — full documentation of the event-driven autonomy pattern

### Changed
- `brain_watcher` watch scope narrowed to DB file paths only (non-recursive) — eliminates
  spurious restarts from `.curator_backups/`, log files, and cache directories
- ISDIR + CREATE restart gated behind whitelist (`kanban/boards/` only)
- `kb_watcher`: `SKILL.md` changes now also trigger `kb_index` rebuild (previously only `skill_kb_bridge`)
- Import order standardised across `cli/main.py`, `providers/publisher.py` (isort)
- CLI evolution log loop variable renamed `e` → `entry` (clarity)
- Test: `inspect path --from main` → `--from nova.cli.main.main` (ambiguity fix after kb_quickstart.main added)

### Fixed
- `nova/inspect` `find_path("main", ...)` — ambiguous when multiple `main` symbols exist after kb_quickstart example was added; now requires fully-qualified qualname

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
