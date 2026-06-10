"""
nova.watcher.brain — Brain Watcher: inotify-based reaction loop.

Watches ``brain.db`` (and ``kanban.db``) for filesystem changes via inotifywait,
then inspects what actually changed and triggers downstream engines.

No polling. No cron. Change → detect → react → silent until next change.

Reaction table
--------------
takes  +5   → learn_engine     (30 min cooldown)
takes  +15  → synthesize        (5 min  cooldown)
takes  +100 → dream_cycle       (2 h    cooldown)
orphan ≥ 3  → fix_orphan        (30 s   cooldown)
health < 90 → dream_cycle       (2 h    cooldown)
kanban done++ → chain_engine    (10 s   cooldown)
MEMORY ≥ 85% → memory_slim     (30 min cooldown)

Cascade reactions (piggyback on primary actions)
-------------------------------------------------
synthesize / dream → wiki crosslink    (6 h  cooldown)
dream             → wiki takes summary (12 h cooldown)
dream             → wiki stale refresh (24 h cooldown, background)
synthesize / dream / learn → RSS update (6 h cooldown)

Usage
-----
Run as a long-lived background process::

    python -m nova.watcher.brain --nova-home ~/.nova

Or integrate with supervisor (see docs/guides/autonomous-event-loop.md).

Requirements
------------
- inotifywait  (inotify-tools package on Linux)
- Python 3.10+

The watcher is Linux-only (inotify). macOS users can substitute kqueue/FSEvents
via a community wrapper; see CONTRIBUTING.md for details.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_home(nova_home: str | None) -> Path:
    raw = nova_home or os.environ.get("NOVA_HOME", "~/.nova")
    return Path(raw).expanduser().resolve()


def _log(msg: str, log_file: Path | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[brain-watcher] [{ts}] {msg}"
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ── state persistence ─────────────────────────────────────────────────────────

def _load_state(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def _save_state(state: dict, state_file: Path) -> None:
    try:
        state_file.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _can_act(state: dict, key: str, min_s: float) -> bool:
    return (time.time() - state.get(f"last_{key}", 0)) >= min_s


# ── brain snapshot ────────────────────────────────────────────────────────────

def _snap_brain(brain_db: Path) -> dict[str, Any] | None:
    try:
        db = sqlite3.connect(str(brain_db), timeout=2)
        c = db.cursor()
        takes = c.execute("SELECT count(*) FROM takes").fetchone()[0]
        orphan = c.execute(
            "SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'"
        ).fetchone()[0]
        open_c = c.execute(
            "SELECT count(*) FROM contradictions WHERE status='open'"
        ).fetchone()[0]
        row = c.execute(
            "SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        health = row[0] if row else 100.0
        db.close()
        return {"takes": takes, "orphan": orphan, "open_contra": open_c, "health": health}
    except Exception:
        return None


def _snap_kanban(kanban_dirs: list[Path]) -> dict[str, int] | None:
    total_done = total_active = 0
    found = False
    for board_dir in kanban_dirs:
        db_path = board_dir / "kanban.db"
        if not db_path.exists():
            continue
        try:
            db = sqlite3.connect(str(db_path), timeout=2)
            c = db.cursor()
            done = c.execute("SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0]
            active = c.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('running','todo','ready')"
            ).fetchone()[0]
            db.close()
            total_done += done
            total_active += active
            found = True
        except Exception:
            pass
    return {"done": total_done, "active": total_active} if found else None


# ── memory snapshot ───────────────────────────────────────────────────────────

def _snap_memory(memory_md: Path, limit: int = 20_000) -> dict:
    try:
        if memory_md.exists():
            chars = len(memory_md.read_text(encoding="utf-8"))
            return {"chars": chars, "pct": int(chars * 100 / limit)}
    except Exception:
        pass
    return {"chars": 0, "pct": 0}


# ── inotify process ───────────────────────────────────────────────────────────

def _watch_dirs(brain_db: Path, kanban_dirs: list[Path]) -> list[str]:
    """Minimal watch targets — only the directories that directly contain DB files.

    Non-recursive to avoid noise from log files, caches, backups, etc.
    """
    targets = [str(brain_db.parent)]
    for d in kanban_dirs:
        if d.exists():
            targets.append(str(d))
    # Deduplicate
    seen: set[str] = set()
    return [t for t in targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]


def _spawn_inotify(watch_dirs: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "inotifywait", "-m",
            # Non-recursive: only watch the exact directories listed,
            # not their subdirectories.  This eliminates noise from
            # .curator_backups/, logs/, cache/, etc.
            "-e", "close_write,create,moved_to,delete",
            "--format", "%w|%f|%e",
            *watch_dirs,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


_DB_FILENAMES = {
    "brain.db", "brain.db-wal", "brain.db-shm",
    "kanban.db", "kanban.db-wal", "kanban.db-shm",
}

# Directories where new subdirectory creation should trigger watcher restart.
# Only kanban/boards/ counts — we might add a new board and need to watch it.
_RESTART_PREFIXES: list[str] = []  # populated at runtime from kanban_root


# ── engine runners ────────────────────────────────────────────────────────────

def _run_bg(cmd: list[str], label: str, log_file: Path | None, timeout: int = 600) -> None:
    """Run a command in a background thread, logging result."""
    def _worker() -> None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = (r.stdout or "").strip().splitlines()
            tail = out[-1][:160] if out else "ok"
            if r.returncode == 0:
                _log(f"  [{label}] OK — {tail}", log_file)
            else:
                err = ((r.stderr or "") + (r.stdout or "")).strip()
                _log(f"  [{label}] ERROR rc={r.returncode} {err[:200]}", log_file)
        except Exception as e:
            _log(f"  [{label}] EXCEPTION {e}", log_file)

    threading.Thread(target=_worker, daemon=True).start()


# ── reaction logic ────────────────────────────────────────────────────────────

REACT = {
    "takes_for_dream":       100,   # +N takes → DreamCycle
    "takes_for_synthesize":   15,   # +N takes → synthesize
    "takes_for_learn":         5,   # +N takes → learn_engine
    "orphan_max":              3,   # orphan ≥ N → fix_orphan
    "health_critical":        90.0, # health < N → DreamCycle
    "chain_min_s":            10,
    "synthesize_min_s":      300,
    "dream_min_s":          7200,   # 2 h
    "learn_min_s":          1800,   # 30 min
    "crosslink_min_s":     21600,   # 6 h
    "takes_wiki_min_s":    43200,   # 12 h
    "stale_wiki_min_s":    86400,   # 24 h
    "memory_check_min_s":   1800,   # 30 min
    "memory_slim_threshold":  85,   # %
    "memory_limit_chars":  20_000,
}


def _react(
    brain_now: dict,
    brain_prev: dict,
    kanban_now: dict | None,
    kanban_prev: dict | None,
    state: dict,
    engines: dict[str, list[str]],
    wiki_synth: Path | None,
    resource_updater: Path | None,
    memory_md: Path | None,
    log_file: Path | None,
) -> list[str]:
    """Decide and execute reactions based on what changed."""
    R = REACT
    acted: list[str] = []
    new_takes = brain_now["takes"] - brain_prev.get("takes", brain_now["takes"])

    # CRITICAL: health drop
    if brain_now["health"] < R["health_critical"]:
        if _can_act(state, "dream", R["dream_min_s"]):
            _log(f"  CRITICAL health={brain_now['health']:.1f} → DreamCycle", log_file)
            if "dream" in engines:
                _run_bg(engines["dream"], "dream_critical", log_file, timeout=700)
                state["last_dream"] = time.time()
                state["takes_at_last_dream"] = brain_now["takes"]
            acted.append("dream_critical")

    # CRITICAL: orphan pages
    if brain_now["orphan"] >= R["orphan_max"] and _can_act(state, "fix_orphan", 30):
        _log(f"  orphan={brain_now['orphan']} → fix_orphan", log_file)
        if "fix_orphan" in engines:
            _run_bg(engines["fix_orphan"], "fix_orphan", log_file, timeout=60)
        state["last_fix_orphan"] = time.time()
        acted.append("fix_orphan")

    # Kanban done → chain_engine
    if kanban_now and kanban_prev:
        new_done = kanban_now["done"] - kanban_prev.get("done", kanban_now["done"])
        if new_done > 0 and _can_act(state, "chain", R["chain_min_s"]):
            _log(f"  kanban done +{new_done} → chain_engine", log_file)
            if "chain" in engines:
                _run_bg(engines["chain"], "chain_engine", log_file, timeout=60)
            state["last_chain"] = time.time()
            acted.append("chain_engine")

    # Takes reactions (tiered)
    if new_takes >= R["takes_for_dream"] and _can_act(state, "dream", R["dream_min_s"]):
        _log(f"  takes +{new_takes} → DreamCycle", log_file)
        if "dream" in engines:
            _run_bg(engines["dream"], "dream_takes", log_file, timeout=700)
            state["last_dream"] = time.time()
            state["takes_at_last_dream"] = brain_now["takes"]
        acted.append("dream_takes")
    elif new_takes >= R["takes_for_synthesize"] and _can_act(state, "synthesize", R["synthesize_min_s"]):
        _log(f"  takes +{new_takes} → synthesize", log_file)
        if "synthesize" in engines:
            _run_bg(engines["synthesize"], "synthesize", log_file, timeout=400)
            state["last_synthesize"] = time.time()
        acted.append("synthesize")
    elif new_takes >= R["takes_for_learn"] and _can_act(state, "learn", R["learn_min_s"]):
        _log(f"  takes +{new_takes} → learn", log_file)
        if "learn" in engines:
            _run_bg(engines["learn"], "learn", log_file, timeout=120)
            state["last_learn"] = time.time()
        acted.append("learn")

    # Cascade: wiki crosslink (after synthesize or dream)
    if any(a in acted for a in ["synthesize", "dream_takes", "dream_critical"]):
        if wiki_synth and _can_act(state, "wiki_crosslink", R["crosslink_min_s"]):
            _run_bg(
                [sys.executable, str(wiki_synth), "--phase", "crosslink"],
                "wiki_crosslink", log_file, timeout=300,
            )
            state["last_wiki_crosslink"] = time.time()
            acted.append("wiki_crosslink")

    # Cascade: wiki takes summary (after dream)
    if any(a in acted for a in ["dream_takes", "dream_critical"]):
        if wiki_synth and _can_act(state, "wiki_takes", R["takes_wiki_min_s"]):
            _run_bg(
                [sys.executable, str(wiki_synth), "--phase", "takes"],
                "wiki_takes", log_file, timeout=300,
            )
            state["last_wiki_takes"] = time.time()
            acted.append("wiki_takes")

        # Cascade: wiki stale refresh (background, heavy)
        if wiki_synth and _can_act(state, "wiki_stale", R["stale_wiki_min_s"]):
            try:
                stale_log = open(str(log_file.parent / "wiki_stale.log") if log_file else "/dev/null", "a")
                subprocess.Popen(
                    [sys.executable, str(wiki_synth), "--phase", "stale"],
                    stdout=stale_log, stderr=subprocess.STDOUT,
                )
                state["last_wiki_stale"] = time.time()
                _log("  [wiki_stale] started background stale refresh", log_file)
            except Exception as e:
                _log(f"  [wiki_stale] failed: {e}", log_file)

    # Cascade: resource update (RSS / external signals)
    if any(a in acted for a in ["synthesize", "dream_takes", "dream_critical", "learn"]):
        if resource_updater and _can_act(state, "resource_update", 6 * 3600):
            _run_bg(
                [sys.executable, str(resource_updater), "--domain", "all"],
                "rss_update", log_file, timeout=120,
            )
            state["last_resource_update"] = time.time()

    # Memory check
    if memory_md and _can_act(state, "memory_check", R["memory_check_min_s"]):
        snap = _snap_memory(memory_md, R["memory_limit_chars"])
        state["last_memory_check"] = time.time()
        state["memory_pct"] = snap["pct"]
        if snap["pct"] >= R["memory_slim_threshold"]:
            _log(f"  MEMORY {snap['pct']}% ≥ {R['memory_slim_threshold']}% → memory_slim", log_file)
            if "memory_slim" in engines:
                _run_bg(engines["memory_slim"], "memory_slim", log_file, timeout=60)
            state["last_memory_slim"] = time.time()
            acted.append(f"memory_slim_{snap['pct']}pct")

    return acted


# ── main watcher loop ─────────────────────────────────────────────────────────

def run(
    nova_home: Path,
    engines: dict[str, list[str]] | None = None,
    verbose: bool = False,
) -> None:
    """Run the brain watcher loop forever (blocking).

    Parameters
    ----------
    nova_home:
        Root NOVA data directory (default ``~/.nova``).
    engines:
        Map of engine name → command list.  Defaults to built-in engine scripts
        under ``nova_home/engines/``.  You can override any or all of them.

        Built-in keys: ``dream``, ``synthesize``, ``learn``, ``chain``,
        ``fix_orphan``, ``memory_slim``.
    verbose:
        Print all inotify events (not just acted ones).
    """
    brain_db = nova_home / "brain.db"
    kanban_root = nova_home / "kanban" / "boards"
    state_file = nova_home / "logs" / "brain_watcher_state.json"
    log_file = nova_home / "logs" / "brain_watcher.log"
    memory_md = nova_home / "memory.md"

    (nova_home / "logs").mkdir(parents=True, exist_ok=True)

    # Resolve kanban board dirs
    def _kanban_dirs() -> list[Path]:
        if not kanban_root.exists():
            return []
        return [d for d in kanban_root.iterdir() if d.is_dir() and (d / "kanban.db").exists()]

    # Engine defaults (override with engines= param)
    engines_dir = nova_home / "engines"
    default_engines: dict[str, list[str]] = {
        "dream":       [sys.executable, str(engines_dir / "dream.py")],
        "synthesize":  [sys.executable, str(engines_dir / "synthesize.py")],
        "learn":       [sys.executable, str(engines_dir / "learn.py")],
        "chain":       [sys.executable, str(engines_dir / "chain.py")],
        "fix_orphan":  [sys.executable, str(engines_dir / "fix_orphan.py")],
        "memory_slim": [sys.executable, str(engines_dir / "memory_slim.py")],
    }
    if engines:
        default_engines.update(engines)
    # Remove engines whose scripts don't exist
    active_engines = {
        k: v for k, v in default_engines.items()
        if Path(v[-1]).exists()
    }

    wiki_synth_path = nova_home / "wiki" / "synthesize.py"
    wiki_synth = wiki_synth_path if wiki_synth_path.exists() else None

    resource_updater_path = nova_home / "engines" / "resource.py"
    resource_updater = resource_updater_path if resource_updater_path.exists() else None

    state = _load_state(state_file)
    board_dirs = _kanban_dirs()
    watch_dirs = _watch_dirs(brain_db, board_dirs)
    kanban_restart_prefix = str(kanban_root) if kanban_root.exists() else None

    brain_prev = _snap_brain(brain_db) or {}
    kanban_prev = _snap_kanban(board_dirs)

    _log("started — inotify event-driven, non-recursive", log_file)
    _log(f"  brain_db: {brain_db}", log_file)
    _log(f"  watch_dirs: {watch_dirs}", log_file)
    if active_engines:
        _log(f"  engines: {list(active_engines.keys())}", log_file)

    while True:
        proc = _spawn_inotify(watch_dirs)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "|" not in line:
                    continue
                watch_dir, filename, events = line.split("|", 2)
                full = Path(watch_dir) / filename

                if verbose:
                    _log(f"  [inotify] {full} {events}", log_file)

                # New subdirectory under kanban/boards/ → restart to pick it up
                if "ISDIR" in events and ("CREATE" in events or "MOVED_TO" in events):
                    if kanban_restart_prefix and str(full).startswith(kanban_restart_prefix):
                        _log(f"  new kanban board detected → restart watcher: {full}", log_file)
                        board_dirs = _kanban_dirs()
                        watch_dirs = _watch_dirs(brain_db, board_dirs)
                        break
                    # Ignore ISDIR events from other directories (backups, cache…)
                    continue

                # Only react to actual DB file changes
                if filename not in _DB_FILENAMES:
                    continue

                brain_now = _snap_brain(brain_db)
                kanban_now = _snap_kanban(board_dirs)
                if brain_now is None:
                    continue

                brain_changed = brain_now != brain_prev
                kanban_changed = kanban_now and kanban_now != kanban_prev

                if not (brain_changed or kanban_changed):
                    continue

                acted = _react(
                    brain_now, brain_prev,
                    kanban_now, kanban_prev,
                    state, active_engines, wiki_synth, resource_updater,
                    memory_md if memory_md.exists() else None,
                    log_file,
                )
                if acted:
                    _log(f"  reacted: {acted}", log_file)
                    _save_state(state, state_file)

                brain_prev = brain_now
                if kanban_now:
                    kanban_prev = kanban_now

        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            time.sleep(1)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NOVA Brain Watcher — inotify-driven autonomous reaction loop"
    )
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory (default: $NOVA_HOME or ~/.nova)",
    )
    parser.add_argument("--verbose", action="store_true", help="Log all inotify events")
    args = parser.parse_args()

    nova_home = _resolve_home(args.nova_home)
    nova_home.mkdir(parents=True, exist_ok=True)

    try:
        run(nova_home=nova_home, verbose=args.verbose)
    except KeyboardInterrupt:
        print("\n[brain-watcher] stopped")


if __name__ == "__main__":
    main()
