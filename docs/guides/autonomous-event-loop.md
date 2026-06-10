# NOVA Autonomous Event Loop

NOVA supports a fully **event-driven autonomous operation** mode — no cron jobs, no polling, just
inotify-based filesystem watching that reacts instantly to real changes.

This guide explains the architecture and how to configure it for your own deployment.

---

## Why Event-Driven?

Cron-based automation has a fundamental problem: **it doesn't know whether something actually changed**.
A cron job that runs every hour wastes resources when there's nothing to do, and delays action when
something happens in between ticks.

The NOVA autonomous loop inverts this:

```
State changes → Detected immediately → React once → Silent otherwise
```

This is the same model that operating systems use for filesystem events (inotify), that databases use
for triggers, and that message queues use for subscriptions. It's reactive, not polling.

---

## Core Components

### 1. Brain Watcher (`nova_brain_watcher.py`)

Watches the central knowledge DB (`nova_brain.db` and `kanban.db`) for changes via inotify.

**Watched paths** — only the DB files themselves (non-recursive, zero noise):
- `$NOVA_HOME/nova_brain.db` (and WAL/SHM sidecars)
- Each registered board's `kanban.db`

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
| dream | `resource collector` (deep) | 7 days |

### 2. KB Watcher (`kb_watcher.py`)

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
- ISDIR + CREATE restart only for whitelisted paths (`kanban/boards/`) — `.curator_backups/` etc. are ignored
- Per-file debounce: 3 s
- Per-action cooldowns: `skill_bridge` 10 s, `wiki_lessons` 15 s, `kb_index` 15 s

### 3. Published Hook Server (`published_hook_server.py`)

A lightweight HTTP server (port 9121) that receives publish-complete webhooks and triggers
downstream sync immediately — no separate cron needed.

```
POST /publish  →  Redis timeline  →  sync_published (10 min cooldown)
                                 →  geo_bible update (6 h, if data changed)
```

---

## Noise Reduction

The most important design decision is **what NOT to watch**.

Bad: `inotifywait -r $NOVA_HOME` — watches everything including log files, backups, caches.
Every write triggers an event. This creates thousands of spurious restarts.

Good: watch only the specific DB files and directories that carry meaningful state:

```python
def _watch_target_dirs() -> list[str]:
    targets = ["$NOVA_HOME"]          # nova_brain.db lives here (top-level only)
    for board_dir in boards_root.iterdir():
        if (board_dir / "kanban.db").exists():
            targets.append(str(board_dir))  # individual board dirs (not recursive)
    return targets
```

With `inotifywait` run **without** `-r`, the kernel only delivers events for files directly inside
the listed directories — not subdirectories. This eliminates all the noise from `.curator_backups/`,
`logs/`, `cache/`, etc.

ISDIR + CREATE events (new subdirectory) are only acted on if the path starts with a
whitelisted prefix — otherwise silently discarded:

```python
_WATCH_DIR_PREFIXES_ALLOWED_RESTART = [
    "$NOVA_HOME/kanban/boards",
]
```

---

## Replacing Cron Jobs

This table shows which NOVA cron jobs were eliminated by the event loop and what replaced them:

| Cron Job | Was | Replaced By |
|---|---|---|
| `nova-dream-nightly` | daily at 18:30 | brain_watcher: takes +100 → DreamCycle |
| `nova-brain-sync-daily` | daily at 02:00 | kb_watcher: KB change → kb_pipeline immediate |
| `skill-kb-bridge-daily` | daily at 20:00 | kb_watcher: SKILL.md change → skill_kb_bridge |
| `nova-wiki-synthesize-weekly` | weekly Mon | kb_watcher: lesson change → wiki_synthesize |
| `kb-index-daily` | daily at 21:00 | kb_watcher: any KB/skill change → kb_index_builder |
| `memory-slim-daily` | daily at 18:00 | brain_watcher: MEMORY ≥ 85% → memory_slim |
| `nova-resource-seo-weekly` | weekly Mon | brain_watcher: dream → resource_collector (7d) |
| `nova-resource-marketing-weekly` | weekly Tue | brain_watcher: dream → resource_collector (7d) |
| `nova-resource-dev-monthly` | monthly | brain_watcher: dream → resource_collector (7d) |
| `hermes-joint-audit-check-12h` | every 12 h | brain_watcher: STAGNANT → audit_loop (12h) |
| `nova-canary-6h` | every 6 h | brain_watcher: health event → canary check |
| `blog-published-sync` | daily 03:00 | published_hook_server: POST /publish → sync (10 min) |
| `geo-bible-auto-update` | daily 22:00 | published_hook_server: POST /publish → geo (6h, if changed) |

**14 cron jobs eliminated. Zero polling loops.**

---

## Implementation Notes

### Cooldown State

All cooldowns are persisted to a JSON state file (`nova_brain_watcher_state.json`).
This means the watcher can restart without "forgetting" when an action last ran.
Cooldowns survive process restarts.

### Cascade Safety

Cascade actions (RSS, wiki, stale) are always gated behind their own `can_act()` cooldown check,
independent of whether the primary action ran. This prevents a DreamCycle triggered by health
from also triggering the 24h stale refresh if it ran 2 hours ago.

### KB Agent Integration

`kb/agents/` is fully included in the kb_watcher watch scope. When a NOVA agent writes its
output to `kb/agents/<name>/`, the change is detected instantly, piped through `kb_pipeline`,
embedded into `nova_brain.db`, and indexed — the brain "sees" the agent's work immediately.

---

## Supervisor Configuration

Both watchers run as supervised processes:

```
nova-brain-watcher  RUNNING  (inotify, non-recursive, DB paths only)
kb-watcher          RUNNING  (inotify, recursive, KB + skills)
```

If a watcher dies (e.g. inotify limit hit), supervisor restarts it automatically.
The state file ensures no cooldown regression on restart.

Check status:
```bash
supervisorctl -c ~/.hermes/supervisor/supervisord.conf status kb-watcher nova-brain-watcher
```
