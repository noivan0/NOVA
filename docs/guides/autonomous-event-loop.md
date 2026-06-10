# NOVA Autonomous Event Loop

NOVA supports a fully **event-driven autonomous operation** mode — no cron jobs, no polling,
just inotify-based filesystem watching that reacts instantly to real changes.

This guide explains the architecture and how to configure it for your own deployment.

---

## Why Event-Driven?

Cron-based automation has a fundamental problem: it doesn't know whether something actually changed.
A cron job that runs every hour wastes resources when there is nothing to do, and delays action when
something happens in between ticks.

The NOVA autonomous loop inverts this:

```
State changes → Detected immediately → React once → Silent otherwise
```

This is the same model that operating systems use for filesystem events (inotify), that databases use
for triggers, and that message queues use for subscriptions. It is reactive, not polling.

---

## Core Components

### 1. Brain Watcher (`nova.watcher.brain`)

Watches the central knowledge database (`brain.db` and `kanban.db`) for changes via inotify.

**Watched paths** — only the directories that directly contain DB files (non-recursive, zero noise):
- `$NOVA_HOME/brain.db` (and WAL/SHM sidecars)
- `$NOVA_HOME/kanban/boards/<board>/kanban.db` — each registered board individually

**Reaction table:**

| Event | Trigger | Cooldown |
|---|---|---|
| takes +5 | `learn_engine` | 30 min |
| takes +15 | `synthesize` | 5 min |
| takes +100 | `DreamCycle` | 2 h |
| orphan ≥ 3 | `fix_orphan` | 30 s |
| health < 90 | `DreamCycle` | 2 h |
| kanban done++ | `chain_engine` | 10 s |
| MEMORY ≥ 85% | `memory_slim` | 30 min |
| STAGNANT agent | `audit_loop` | 12 h |

**Cascade (piggyback) reactions** triggered after primary actions:

| Primary | Cascade | Cooldown |
|---|---|---|
| synthesize / dream | `wiki crosslink` | 6 h |
| dream | `wiki takes summary` | 12 h |
| dream | `wiki stale refresh` | 24 h |
| synthesize / dream / learn | `RSS resource update` | 6 h |

### 2. KB Watcher (`nova.watcher.kb`)

Watches the KB and skills directories via inotify (recursive):
- `$NOVA_HOME/kb/` — all subdirectories including `kb/agents/`
- `$NOVA_HOME/skills/` — skill SKILL.md files

**Reaction table:**

| File event | Action |
|---|---|
| `kb/*.md` changed | `kb_pipeline` (embed + brain sync) + `kb_index` rebuild |
| `kb/lessons/*.md` changed | `wiki_synthesize --phase lessons,index` |
| `skills/*/SKILL.md` changed | `skill_kb_bridge` (embed) + `kb_index` rebuild |
| Any KB file added/deleted | `kb_index` rebuild |

**Noise suppression:**
- Per-file debounce: 3 s
- Per-action cooldowns: `skill_bridge` 10 s, `wiki_lessons` 15 s, `kb_index` 15 s

### 3. Published Hook Server (`nova.watcher.hook_server`)

A lightweight HTTP server (port 9121) that receives publish-complete webhooks and triggers
downstream sync immediately — no separate cron needed.

```
POST /publish  →  Redis timeline  →  sync_published (10 min cooldown)
                                 →  geo_update (6 h, if data changed)
```

---

## Noise Reduction

The most important design decision is **what NOT to watch**.

Bad pattern — recursive on the entire data directory:

```python
# Watches everything including log files, backups, caches.
# Every write triggers an event. Thousands of spurious restarts.
subprocess.Popen(["inotifywait", "-m", "-r", "-e", "...", "$NOVA_HOME"])
```

Good pattern — non-recursive, only the exact directories containing DB files:

```python
def _watch_dirs(brain_db: Path, kanban_dirs: list[Path]) -> list[str]:
    targets = [str(brain_db.parent)]        # brain.db lives here
    for d in kanban_dirs:                   # each board directory individually
        if d.exists():
            targets.append(str(d))
    return targets
```

With `inotifywait` run **without** `-r`, the kernel only delivers events for files directly
inside the listed directories — not subdirectories. This eliminates all noise from backup
directories, log rotations, caches, etc.

ISDIR + CREATE events (new subdirectory) are only acted on if the path is under
`$NOVA_HOME/kanban/boards/` — a new board directory we need to start watching.
All other ISDIR events are silently discarded:

```python
if "ISDIR" in events and ("CREATE" in events or "MOVED_TO" in events):
    if str(full).startswith(str(kanban_root)):
        # New board → restart watcher to pick it up
        break
    else:
        continue   # Ignore: backups, caches, etc.
```

---

## Replacing Cron Jobs

This table shows which cron jobs the event loop replaces:

| Cron Job | Was | Replaced By |
|---|---|---|
| `nova-dream-nightly` | daily at 18:30 | brain_watcher: takes +100 → DreamCycle |
| `nova-brain-sync-daily` | daily at 02:00 | kb_watcher: KB change → kb_pipeline immediate |
| `skill-kb-bridge-daily` | daily at 20:00 | kb_watcher: SKILL.md change → skill_kb_bridge |
| `nova-wiki-synthesize-weekly` | weekly Mon | kb_watcher: lesson change → wiki_synthesize |
| `kb-index-daily` | daily at 21:00 | kb_watcher: any KB/skill change → kb_index |
| `memory-slim-daily` | daily at 18:00 | brain_watcher: MEMORY ≥ 85% → memory_slim |
| `blog-published-sync` | daily 03:00 | hook_server: POST /publish → sync_published |
| `geo-update-daily` | daily 22:00 | hook_server: POST /publish → geo_update (if changed) |

**Zero polling loops.**

---

## Quick Start

Install NOVA and start the watchers:

```bash
pip install nova-orchestrator
nova watcher start --nova-home ~/.nova
```

Or run each watcher individually:

```bash
# Terminal 1 — brain watcher
python -m nova.watcher.brain --nova-home ~/.nova

# Terminal 2 — KB watcher
python -m nova.watcher.kb --nova-home ~/.nova

# Terminal 3 — hook server (optional, for publish events)
python -m nova.watcher.hook_server --nova-home ~/.nova --port 9121
```

With supervisor (recommended for production):

```ini
[program:nova-brain-watcher]
command=python -m nova.watcher.brain --nova-home %(ENV_NOVA_HOME)s
autorestart=true
stdout_logfile=%(ENV_NOVA_HOME)s/logs/brain_watcher.log

[program:nova-kb-watcher]
command=python -m nova.watcher.kb --nova-home %(ENV_NOVA_HOME)s
autorestart=true
stdout_logfile=%(ENV_NOVA_HOME)s/logs/kb_watcher.log
```

---

## Implementation Notes

### Cooldown State

All cooldowns are persisted to `$NOVA_HOME/logs/brain_watcher_state.json`.
The watcher can restart without "forgetting" when an action last ran.
Cooldowns survive process restarts.

### Cascade Safety

Cascade actions (wiki, RSS) are gated behind their own cooldown check,
independent of whether the primary action ran. A DreamCycle triggered by health drop
will not also trigger the 24 h stale refresh if it ran 2 hours ago.

### KB Agent Integration

`$NOVA_HOME/kb/agents/` is included in the KB watcher scope. When a NOVA agent writes
its output to `kb/agents/<name>/`, the change is detected instantly, piped through
`kb_pipeline`, embedded into `brain.db`, and indexed — the brain sees the agent's work
immediately without any polling.

### Platform Support

The watchers require `inotifywait` (Linux only). Install via:

```bash
# Debian/Ubuntu
sudo apt-get install inotify-tools

# Fedora/RHEL
sudo dnf install inotify-tools
```

macOS users can use [fswatch](https://github.com/emcrisostomo/fswatch) as a drop-in
replacement by overriding the `_spawn_inotify` function. A community adapter is
welcome as a pull request — see CONTRIBUTING.md.
