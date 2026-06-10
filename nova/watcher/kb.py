"""
nova.watcher.kb — KB Watcher: inotify-driven knowledge base sync loop.

Watches two directory trees for file changes:

  ~/.nova/kb/       — knowledge base markdown files
  ~/.nova/skills/   — skill definition files (SKILL.md)

On change, immediately triggers the appropriate pipeline:

  KB *.md changed    → kb_pipeline (embed + brain sync) + kb_index rebuild
  lesson KB changed  → wiki synthesize (lessons + index phases)
  SKILL.md changed   → skill_kb_bridge + kb_index rebuild
  KB file deleted    → kb_index rebuild

Design: inotify recursive (-r) on both trees, with debounce and
per-action global cooldowns to avoid thundering herds.

Usage::

    python -m nova.watcher.kb --nova-home ~/.nova

Requirements: inotifywait (Linux), Python 3.10+.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


# ── logging ───────────────────────────────────────────────────────────────────

def _log(msg: str, log_file: Path | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[kb-watcher] [{ts}] {msg}"
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ── constants ─────────────────────────────────────────────────────────────────

EXCLUDE_FILES: set[str] = {
    "index.md", "INDEX.md", "log.md", "SCHEMA.md",
    "_registry.md", "TEMPLATE.md", "memory_pending.md",
}

EXCLUDE_DIRS: set[str] = {"archive", "weekly", "__pycache__"}

# Per-file debounce: don't re-trigger the same file within N seconds
FILE_DEBOUNCE_S = 3.0

# Global cooldowns per action type
GLOBAL_COOLDOWN: dict[str, float] = {
    "skill_bridge": 10.0,
    "wiki_lessons": 15.0,
    "kb_index":     15.0,
}

# ── debounce state ────────────────────────────────────────────────────────────

_last_file: dict[str, float] = {}
_last_global: dict[str, float] = {}
_lock = threading.Lock()


def _file_ok(key: str) -> bool:
    now = time.time()
    with _lock:
        if now - _last_file.get(key, 0) < FILE_DEBOUNCE_S:
            return False
        _last_file[key] = now
    return True


def _global_ok(key: str) -> bool:
    cooldown = GLOBAL_COOLDOWN.get(key, 10.0)
    now = time.time()
    with _lock:
        if now - _last_global.get(key, 0) < cooldown:
            return False
        _last_global[key] = now
    return True


# ── background runner ─────────────────────────────────────────────────────────

def _run_bg(cmd: list[str], label: str, log_file: Path | None, timeout: int = 600) -> None:
    def _worker() -> None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                out = (r.stdout or "").strip().splitlines()
                tail = out[-1][:160] if out else "ok"
                _log(f"  [{label}] OK — {tail}", log_file)
            else:
                err = ((r.stderr or "") + (r.stdout or "")).strip()
                _log(f"  [{label}] ERROR rc={r.returncode} {err[:200]}", log_file)
        except Exception as e:
            _log(f"  [{label}] EXCEPTION {e}", log_file)

    threading.Thread(target=_worker, daemon=True).start()


# ── path classification ───────────────────────────────────────────────────────

def _is_excluded(path: Path, kb_root: Path) -> bool:
    try:
        rel = path.relative_to(kb_root)
    except ValueError:
        return True
    if path.name in EXCLUDE_FILES:
        return True
    return any(part in EXCLUDE_DIRS for part in rel.parts)


def _is_kb_md(path: Path, kb_root: Path) -> bool:
    return path.suffix == ".md" and path.exists() and not _is_excluded(path, kb_root)


def _is_skill_file(path: Path, skills_root: Path) -> bool:
    try:
        path.relative_to(skills_root)
    except ValueError:
        return False
    return path.name == "SKILL.md" and path.exists()


def _is_lessons(path: Path, kb_root: Path) -> bool:
    try:
        rel = path.relative_to(kb_root)
        return bool(rel.parts) and rel.parts[0] == "lessons"
    except ValueError:
        return False


# ── triggers ──────────────────────────────────────────────────────────────────

def _trigger_kb_pipeline(
    path: Path, kb_root: Path,
    pipeline_script: Path | None,
    log_file: Path | None,
) -> None:
    key = f"pipeline:{path.resolve()}"
    if not _file_ok(key):
        return
    rel = path.relative_to(kb_root)
    _log(f"  [KB] CHANGED {rel}", log_file)
    if pipeline_script and pipeline_script.exists():
        _run_bg([sys.executable, str(pipeline_script), str(path)], "kb_pipeline", log_file)


def _trigger_skill_bridge(
    path: Path, skills_root: Path,
    bridge_script: Path | None,
    log_file: Path | None,
) -> None:
    key = f"skill:{path.resolve()}"
    if not _file_ok(key):
        return
    rel = path.relative_to(skills_root)
    _log(f"  [SKILL] CHANGED {rel}", log_file)
    if bridge_script and bridge_script.exists():
        _run_bg([sys.executable, str(bridge_script), "--file", str(path)], "skill_bridge", log_file)


def _trigger_wiki_lessons(wiki_synth: Path | None, log_file: Path | None) -> None:
    if not _global_ok("wiki_lessons"):
        return

    def _worker() -> None:
        if not wiki_synth or not wiki_synth.exists():
            return
        try:
            subprocess.run(
                [sys.executable, str(wiki_synth), "--phase", "lessons"],
                capture_output=True, text=True, timeout=600,
            )
            subprocess.run(
                [sys.executable, str(wiki_synth), "--phase", "index"],
                capture_output=True, text=True, timeout=300,
            )
            _log("  [wiki_lessons] OK — lessons + index", log_file)
        except Exception as e:
            _log(f"  [wiki_lessons] EXCEPTION {e}", log_file)

    threading.Thread(target=_worker, daemon=True).start()


def _trigger_kb_index(index_script: Path | None, log_file: Path | None) -> None:
    if not _global_ok("kb_index"):
        return
    if index_script and index_script.exists():
        _run_bg([sys.executable, str(index_script)], "kb_index", log_file, timeout=300)


# ── inotify process ───────────────────────────────────────────────────────────

def _spawn_inotify(kb_root: Path, skills_root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "inotifywait", "-m", "-r",
            "-e", "close_write,create,moved_to,delete",
            "--format", "%w|%f|%e",
            str(kb_root),
            str(skills_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


# ── main watcher loop ─────────────────────────────────────────────────────────

def run(nova_home: Path) -> None:
    """Run the KB watcher loop forever (blocking)."""
    kb_root = nova_home / "kb"
    skills_root = nova_home / "skills"
    log_file = nova_home / "logs" / "kb_watcher.log"

    # Optional pipeline scripts — use built-in nova engines if they exist,
    # otherwise accept external scripts via env vars.
    engines_dir = nova_home / "engines"
    pipeline_script = _resolve_script(
        engines_dir / "kb_pipeline.py",
        os.environ.get("NOVA_KB_PIPELINE"),
    )
    bridge_script = _resolve_script(
        engines_dir / "skill_kb_bridge.py",
        os.environ.get("NOVA_SKILL_BRIDGE"),
    )
    wiki_synth = _resolve_script(
        nova_home / "wiki" / "synthesize.py",
        os.environ.get("NOVA_WIKI_SYNTH"),
    )
    index_script = _resolve_script(
        engines_dir / "kb_index.py",
        os.environ.get("NOVA_KB_INDEX"),
    )

    (nova_home / "logs").mkdir(parents=True, exist_ok=True)
    kb_root.mkdir(parents=True, exist_ok=True)
    skills_root.mkdir(parents=True, exist_ok=True)

    _log(f"started — event-driven (kb={kb_root}, skills={skills_root})", log_file)

    while True:
        proc = _spawn_inotify(kb_root, skills_root)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "|" not in line:
                    continue
                watch_dir, filename, events = line.split("|", 2)
                full = Path(watch_dir) / filename

                # New directory → restart inotifywait to pick it up recursively
                if "ISDIR" in events and ("CREATE" in events or "MOVED_TO" in events):
                    _log(f"  new directory detected → restart: {full}", log_file)
                    break

                # Delete → rebuild index only
                if "DELETE" in events:
                    try:
                        if kb_root in full.parents and full.suffix == ".md":
                            _log(f"  [KB] DELETE {full.relative_to(kb_root)}", log_file)
                            _trigger_kb_index(index_script, log_file)
                    except Exception:
                        pass
                    continue

                # KB markdown changed
                if _is_kb_md(full, kb_root):
                    _trigger_kb_pipeline(full, kb_root, pipeline_script, log_file)
                    _trigger_kb_index(index_script, log_file)
                    if _is_lessons(full, kb_root):
                        _trigger_wiki_lessons(wiki_synth, log_file)
                    continue

                # Skill file changed
                if _is_skill_file(full, skills_root):
                    _trigger_skill_bridge(full, skills_root, bridge_script, log_file)
                    _trigger_kb_index(index_script, log_file)
                    continue

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


def _resolve_script(default: Path, env_override: str | None) -> Path | None:
    if env_override:
        p = Path(env_override).expanduser()
        return p if p.exists() else None
    return default if default.exists() else None


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NOVA KB Watcher — inotify-driven knowledge base sync loop"
    )
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory (default: $NOVA_HOME or ~/.nova)",
    )
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    nova_home.mkdir(parents=True, exist_ok=True)

    try:
        run(nova_home)
    except KeyboardInterrupt:
        print("\n[kb-watcher] stopped")


if __name__ == "__main__":
    main()
