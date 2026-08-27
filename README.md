# NOVA — Full Autonomous Agent System

> **NOVA is not a framework. It is a living operating system for AI agents.**

NOVA runs 24/7 without timers. It watches its own memory (SQLite DB), detects changes, and reacts instantly. Every agent knows what every other agent has learned. The system grows smarter by itself.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.6.0-orange)](CHANGELOG.md)

---

## What NOVA Does

```
외부 입력 → hermes_events (DB) → brain_watcher 감지 → 에이전트 디스패치
                ↑                                              ↓
         nova_brain.db ←────── takes / pages / learn ←────────┘
         (기억이 변화)          (결과 저장)            (자율 실행)
```

1. **Event-driven**: No polling loops. inotify watches `nova_brain.db` — when memory changes, agents react immediately.
2. **Self-learning**: Every execution writes `takes` (judgments) and `learn` entries back to the DB. The system compounds.
3. **Multi-agent**: 36 specialized agents across bin/ and scripts/. Each has a single responsibility.
4. **KB-linked**: Markdown knowledge base auto-syncs with the DB. Agents can read, write, and cross-link KB pages.
5. **SOUL-driven**: Each agent has a `SOUL.md` — a declarative identity file that defines its role, tools, and mission.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/noivan0/NOVA
cd NOVA

# 2. Install + initialize
bash setup.sh

# 3. Set your API key
nano ~/.hermes/.env

# 4. Start the brain watcher (event engine)
export HERMES_HOME=$HOME/.hermes
python3 $HERMES_HOME/scripts/nova_brain_watcher.py &

# 5. Check system health
python3 $HERMES_HOME/scripts/nova_hermes_briefing.py

# 6. Run autonomous engine
python3 $HERMES_HOME/scripts/nova_autonomous_engine.py
```

---

## Architecture

```
NOVA Architecture
─────────────────────────────────────────────────────────
                    ┌─────────────────┐
                    │   Orchestrator  │  (Hermes Agent / LLM)
                    │   (두뇌 — Brain) │
                    └────────┬────────┘
                             │ judges · decides · delegates
               ┌─────────────┼────────────────┐
               ▼             ▼                ▼
       ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
       │  nova_dream  │ │nova_learn_   │ │nova_chain_   │
       │  (판단 생성)  │ │engine(학습)  │ │engine(연결)  │
       └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
              │                │                 │
              └────────────────┼─────────────────┘
                               ▼
                    ┌─────────────────┐
                    │  nova_brain.db  │  ← 기억 (Memory)
                    │  pages / takes  │
                    │  events / learn │
                    └────────┬────────┘
                             │ inotify CLOSE_WRITE
                             ▼
                    ┌─────────────────┐
                    │nova_brain_watch │  ← 감시 (Watcher)
                    │er.py            │
                    └────────┬────────┘
                             │ dispatches
                    ┌────────┴────────┐
              agents react immediately
```

---

## Full Agent List (36 agents)

### Core Brain Agents — `nova/agents/bin/`

| Agent | Role |
|-------|------|
| `nova_brain.py` | DB CRUD, embedding search, index-all |
| `nova_brain_cli.py` | CLI (takes add, pages search, health) |
| `nova_brain_embed.py` | Vector embedding similarity search |
| `nova_brain_hook.py` | DB change hook trigger |
| `nova_brain_schema.py` | Schema init / migration |
| `nova_brain_synthesize.py` | Synthesize takes → high-level judgment |
| `nova_calibration.py` | Auto-calibration (weight tuning) |
| `nova_codex_gate.py` | Code execution delegation gate |
| `nova_doctor.py` | System health diagnosis |
| `nova_dream.py` | Generate dream takes (top-level insight) |
| `nova_emotional.py` | Tone / emotional layer |
| `nova_kb_claim_extract.py` | Extract claims/facts from KB |
| `nova_kb_sync.py` | KB ↔ nova_brain.db sync |
| `nova_learn_harvester.py` | nova_learn → KB conversion |
| `nova_llm.py` | LLM abstraction layer |
| `nova_migrate_ct.py` | DB migration utility |
| `nova_on_done_takes.py` | Post-takes completion hook |
| `nova_search.py` | Unified pages/takes search |
| `nova_takes_agent.py` | Takes autonomous agent (HIGH priority) |
| `nova_wiki_synthesize.py` | Auto wiki synthesis |

### Autonomous Engine Agents — `nova/agents/scripts/`

| Agent | Role |
|-------|------|
| `nova_brain_watcher.py` | **Core** — inotify event engine |
| `nova_autonomous_engine.py` | Full autonomy pipeline (5 phases) |
| `nova_chain_engine.py` | Agent-to-agent signal relay |
| `nova_learn_engine.py` | Learning pipeline |
| `nova_resource_collector.py` | External resource collection |
| `nova_hermes_briefing.py` | Session start health briefing |
| `nova_phase0.py` | First-run initialization |
| `nova_growth_tracker.py` | Growth metric tracking |
| `nova_kanban_hook.py` | Kanban task completion hook |
| `nova_resource_updater.py` | Resource update agent |
| `nova_brain_watchdog.py` | DB health watchdog |
| `nova_autonomous_loop.py` | Simple loop (dev/test) |
| `nova_autonomous_engine_daemon.py` | Daemon wrapper |
| `nova_db_status.py` | Quick DB status check |
| `nova_kb_sync.py` | Scripts-side KB sync |
| `nova_codex_gate.py` | Scripts-side code gate |

---

## nova_brain.db — Memory Schema

```sql
-- Judgments and insights
takes (id, agent, take, context, importance, category, created_at)

-- Knowledge pages
pages (id, path, title, content, category, weight, auto_link, created_at)

-- Event bus
hermes_events (id, event_type, payload, source, processed, created_at)

-- Learning feed
nova_learn (id, source, content, quality_score, created_at)
```

### KB Hierarchy (L1~L8)

```
L1 dream        Top-level insights (highest abstraction)
L2 synthesize   Multi-takes synthesis
L3 takes        Individual judgment records
L4 learn        Raw learning data
L5 kb           Markdown KB pages
L6 wiki         Cross-linked concept wiki
L7 resources    External collected data
L8 chain        Agent chain execution logs
```

---

## Agent Profiles (SOUL.md)

Each agent has a profile directory:

```
$HERMES_HOME/profiles/{agent-name}/
  SOUL.md       Identity, role, tools, mission
  harness.md    Task harness / prompt template
  evolution.md  Learning history
  config.yaml   LLM config (model, provider, api_key)
```

Example `SOUL.md`:

```markdown
# Nova Research Agent

role: researcher
specialty: web research, arxiv, KB building
tools: [web_search, web_extract, read_file, write_file]

## Mission
Accumulate knowledge in KB and supply learning data to nova_brain.db.

## Principles
1. Verify before storing — no hallucinated facts in KB
2. Every session ends with a takes entry
3. Cross-link related KB pages
```

---

## Workflow — KB Auto-Pipeline

```
External trigger (cron / LLM / hook)
    ↓
resource_collector.py  →  외부 데이터 수집
    ↓
learn_engine.py        →  nova_learn에 저장
    ↓
nova_brain_watcher     →  learn-done 이벤트 감지
    ↓
learn_harvester.py     →  nova_learn → KB 변환
    ↓
wiki_synthesize.py     →  KB 교차연결 + wiki 생성
    ↓
brain_synthesize.py    →  wiki → takes 합성
    ↓
dream.py               →  takes → dream (최고수준 판단)
    ↓
nova_brain.db          →  기억 축적 완료
```

---

## Running 24/7 (systemd)

```ini
# /etc/systemd/system/nova-watcher.service
[Unit]
Description=NOVA Brain Watcher
After=network.target

[Service]
Type=simple
User=YOUR_USER
Environment=HERMES_HOME=/home/YOUR_USER/.hermes
ExecStart=/usr/bin/python3 /home/YOUR_USER/.hermes/scripts/nova_brain_watcher.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable nova-watcher
sudo systemctl start nova-watcher
sudo systemctl status nova-watcher
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Root directory for all NOVA data |
| `HERMES_API_KEY` | — | LLM API key |
| `HERMES_BASE_URL` | `https://api.openai.com/v1` | LLM endpoint |
| `HERMES_MODEL` | `gpt-4o` | Default model |
| `NOVA_LLM_PROVIDER` | `openai` | `openai` \| `anthropic` \| `ollama` \| `custom` \| `groq` \| `deepseek` \| `mistral` \| `xai` \| `moonshot` \| `zhipu` \| `openrouter` \| `together` \| `fireworks` \| `perplexity` \| `echo` — see [Providers](docs/guides/providers.md) |
| `NOVA_LLM_FALLBACK_CHAIN` | — | Optional multi-provider fallback chain, e.g. `openai:gpt-4o,groq:llama-3.3-70b-versatile` |
| `TELEGRAM_BOT_TOKEN` | — | Optional: Telegram notifications |
| `OPENAI_API_KEY` | — | Optional: Codex gate |

---

## Directory Layout

```
$HERMES_HOME/
  nova_brain.db          Main memory database
  .env                   API keys and config
  bin/                   Core brain agents (20 files)
  scripts/               Autonomous engine agents (16 py + 9 sh)
  kb/                    Markdown knowledge base
    config/
    agents/
    projects/
    nova/learnings/
    audit_loop/
  wiki/                  Cross-linked concept wiki
  profiles/              Agent SOUL.md directories
  logs/                  Runtime logs
  kanban/                Task boards
  ipc/                   Inter-agent communication
```

---

## Built-in OSS Engines (nova/engine/)

The `nova/` package ships a clean Python API for programmatic use:

```python
from nova.engine.dream import DreamEngine
from nova.engine.learn import LearnEngine
from nova.engine.chain import ChainEngine
from nova.db.brain import NovaBrain
from nova.watcher.brain import BrainWatcher

# Initialize brain
brain = NovaBrain()

# Start watcher
watcher = BrainWatcher(brain)
watcher.start()
```

---

## Guides

- [Quickstart](docs/guides/quickstart.md)
- [Full Autonomy Architecture](docs/guides/full-autonomy.md)
- [Writing Harnesses](docs/guides/writing-harnesses.md)
- [Custom Providers](docs/guides/custom-provider.md)
- [Quality Gates](docs/guides/quality-gates.md)
- [Autonomous Event Loop](docs/guides/autonomous-event-loop.md)

---

## Philosophy

> NOVA is not woken by timers. It is woken by memory.
>
> When memory changes → agents react.
> When agents act → memory changes.
> When memory grows → judgments improve.
>
> This is the cycle. No cron. No polling. Just signal and response.

---

## License

MIT — see [LICENSE](LICENSE)

Built with [Hermes Agent](https://hermes-agent.nousresearch.com) · Powered by [Nous Research](https://nousresearch.com)
