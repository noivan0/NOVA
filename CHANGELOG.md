# NOVA v1.4.0 — Full Autonomous Release

## What's New

### Major: Full Agent Implementation Published

All 36 internal agents are now publicly available under `nova/agents/`:

**Core Brain Agents (nova/agents/bin/) — 20 agents**
- nova_brain.py — DB CRUD, embedding search, index-all
- nova_brain_cli.py — CLI interface
- nova_brain_embed.py — Vector embedding similarity search
- nova_brain_synthesize.py — takes → high-level synthesis
- nova_codex_gate.py — Code execution delegation
- nova_dream.py — Dream takes generation (top-level insight)
- nova_emotional.py — Tone/emotional layer
- nova_kb_claim_extract.py — KB claim extraction
- nova_kb_sync.py — KB ↔ DB sync
- nova_learn_harvester.py — Learn → KB conversion
- nova_takes_agent.py — Autonomous takes agent
- nova_wiki_synthesize.py — Wiki auto-synthesis
- nova_calibration.py, nova_doctor.py, nova_llm.py, nova_search.py, etc.

**Autonomous Engine Agents (nova/agents/scripts/) — 16 agents**
- nova_brain_watcher.py — **Core event engine** (inotify-based)
- nova_autonomous_engine.py — Full autonomy pipeline
- nova_chain_engine.py — Agent-to-agent relay
- nova_learn_engine.py — Learning pipeline
- nova_resource_collector.py — External resource collection
- nova_hermes_briefing.py — Session start briefing
- nova_growth_tracker.py, nova_kanban_hook.py, etc.

**Shell Scripts (nova/agents/shells/) — 9 scripts**
- nova_audit_loop.sh, nova_dream_runner.sh, nova_evaluator_daily.sh, etc.

### New: setup.sh
One-command installation for any Unix system:
```bash
git clone https://github.com/noivan0/NOVA && cd NOVA && bash setup.sh
```

### New: Full Autonomy Guide
`docs/guides/full-autonomy.md` — Complete architecture documentation including:
- Event flow diagrams
- DB schema reference
- KB hierarchy (L1~L8)
- Agent profile (SOUL.md) spec
- Systemd service setup
- Troubleshooting guide

### Changed: Path Portability
All agents now use `HERMES_HOME` environment variable instead of hardcoded `/root/.hermes`. Default: `~/.hermes`.

---

## v1.3.0 — OSS Framework Release

Initial public release with:
- nova Python package (engine, db, watcher, kb, inspect)
- 6 built-in engines (dream, learn, synthesize, chain, fix_orphan, memory_slim)
- nova setup / nova watcher CLI
- 82 tests passing
