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

try:
    from nova.watcher.cron_engine import cron_tick as _cron_tick
    _CRON_ENGINE_AVAILABLE = True
except Exception:
    _CRON_ENGINE_AVAILABLE = False
    def _cron_tick(*args, **kwargs) -> list:  # type: ignore[misc]
        return []


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_home(nova_home: str | None) -> Path:
    raw = nova_home or os.environ.get("NOVA_HOME", "~/.nova")
    return Path(raw).expanduser().resolve()


def _log(msg: str, log_file: Path | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[brain-watcher] [{ts}] {msg}"
    # BUG-LOG-DUP 수정: brain_watcher 실행 시 stdout → log_file redirect됨
    # print() + file.write() 이중 기록으로 모든 로그가 2번씩 찍히는 문제
    # → log_file 없을 때만 print (디버그), log_file 있으면 파일에만 기록
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    else:
        print(line, flush=True)


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
        # URI read-only 연결 — WAL checkpoint 트리거 방지 (자기 피드백 루프 방지)
        uri = f"file:{brain_db}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=2)
        db.execute("PRAGMA query_only=ON")
        c = db.cursor()
        takes = c.execute("SELECT count(*) FROM takes").fetchone()[0]
        orphan = c.execute(
            "SELECT count(*) FROM pages WHERE agent IS NULL"
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


_snap_kanban_cache: dict = {}  # {path: (result, ts)}
_SNAP_KANBAN_TTL = 3.0  # 3초 캐시 — 빠른 연속 이벤트 시 SQLite 재쿼리 방지

def _invalidate_kanban_cache(kanban_dirs: list[Path]) -> None:
    """kanban.db CLOSE_WRITE 이벤트 수신 시 캐시 무효화 — 최신 값 보장."""
    for board_dir in kanban_dirs:
        cache_key = str(board_dir / "kanban.db")
        _snap_kanban_cache.pop(cache_key, None)

def _snap_kanban(kanban_dirs: list[Path]) -> dict[str, int] | None:
    total_done = total_active = 0
    found = False
    now = time.time()
    for board_dir in kanban_dirs:
        db_path = board_dir / "kanban.db"
        if not db_path.exists():
            continue
        # TTL 캐시: 같은 DB를 3초 이내 재쿼리 방지
        cache_key = str(db_path)
        if cache_key in _snap_kanban_cache:
            cached_result, cached_ts = _snap_kanban_cache[cache_key]
            if now - cached_ts < _SNAP_KANBAN_TTL:
                if cached_result is not None:
                    total_done   += cached_result["done"]
                    total_active += cached_result["active"]
                    found = True
                continue
        try:
            db = sqlite3.connect(str(db_path), timeout=2)
            c = db.cursor()
            done = c.execute("SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0]
            active = c.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('running','todo','ready')"
            ).fetchone()[0]
            db.close()
            result = {"done": done, "active": active}
            _snap_kanban_cache[cache_key] = (result, now)
            total_done += done
            total_active += active
            found = True
        except Exception:
            _snap_kanban_cache[cache_key] = (None, now)
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
    # WAL/SHM 파일 제외 — read-only 연결 시에도 shm/wal 파일 open이
    # inotify CLOSE_WRITE를 트리거해 자기 피드백 루프 유발.
    # 실제 DB 변경은 brain.db 메인 파일에서만 감지하면 충분.
    # BUG-WAL-SPIN: kanban.db-wal/shm을 감시하면 마라톤 진행 중 매우 빈번한 이벤트 발생
    # → _snap_kanban 매 이벤트마다 쿼리 → CPU 99.8% 지속. 메인 파일만 감시.
    "brain.db",
    "kanban.db",
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
                _log(f"  [{label}] ERROR rc={r.returncode} {err[:2000]}", log_file)
        except Exception as e:
            _log(f"  [{label}] EXCEPTION {e}", log_file)

    # chain_engine: 긴 작업(orchestrator --wait 포함) → non-daemon으로 brain_watcher 재시작에도 생존
    # 다른 엔진: daemon=True (빠른 작업이므로 brain_watcher 재시작 시 정리 OK)
    is_chain = "chain" in label
    threading.Thread(target=_worker, daemon=not is_chain).start()


def _apply_master_key_llm_defaults(master_key: str, nova_yaml_path: Path | None = None) -> None:
    """Master API key 발견 시 LLM provider/model env 기본값을 채운다.

    P1 fix (2026-08-18, Codex-audited round 2): 이 함수는 원래
    NOVA_LLM_PROVIDER=hmg를 무조건 선택했는데, HMGProvider가 base_url 없이는
    ValueError를 던지도록 강화된 후(nova/providers/llm.py) NOVA_LLM_BASE_URL이
    설정되지 않은 환경(전형적으로 direct brain-watcher 실행)에서 즉시
    크래시하는 회귀를 만들었다 — Codex cold audit이 실제로 재현해 발견.
    NOVA_LLM_BASE_URL이 이미 설정된 경우에만 hmg를 선택하고, 그렇지 않으면
    API 키 없이 동작하는 공개 echo provider로 폴백한다.

    P1 fix round 3 (같은 날, Codex 재감사): 최초 수정은 os.environ만 확인해서
    "base_url이 nova.yaml에만 설정되고 환경변수로는 없는" 흔한 케이스에서
    잘못 echo를 선택했다. 이 함수가 os.environ["NOVA_LLM_PROVIDER"]="echo"를
    setdefault로 심으면, 이후 load_config()의 _apply_env()가 (yaml보다
    나중에 적용되는) 이 env var를 최우선으로 적용해 YAML에 명시적으로 적어둔
    provider: hmg 설정 자체를 통째로 무시해버린다 — Codex가 실제 재현 스크립트로
    입증(session 20260818_112743_12a498). nova_yaml_path를 받아 YAML의
    llm.base_url도 함께 확인하도록 수정.

    함수로 분리한 이유: 순수 로직(입력=환경/설정 상태, 출력=env 변경)만 독립
    테스트하기 위함 — 원래는 _run_harness_bg() 내부 클로저에 인라인되어 있어
    단위 테스트가 불가능했다.
    """
    has_base_url = bool(os.environ.get("NOVA_LLM_BASE_URL"))
    if not has_base_url and nova_yaml_path is not None:
        try:
            import yaml as _yaml
            if nova_yaml_path.exists():
                _raw = _yaml.safe_load(nova_yaml_path.read_text()) or {}
                has_base_url = bool((_raw.get("llm") or {}).get("base_url"))
        except Exception:
            pass  # YAML을 못 읽어도 echo 폴백으로 안전하게 계속 진행

    if has_base_url:
        os.environ.setdefault("NOVA_LLM_PROVIDER", "hmg")
        os.environ.setdefault("NOVA_LLM_MODEL", "claude-sonnet-4-6")
    else:
        os.environ.setdefault("NOVA_LLM_PROVIDER", "echo")
    # setdefault 대신 강제 덮어쓰기: 셸에서 구키가 export된 채로
    # watcher를 시작해도 .env/.config.yaml의 최신 키가 항상 우선함
    for _var in ("NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY",
                 "NOVA_CODEX_API_KEY", "NOVA_IMAGE_GEN_API_KEY",
                 "HERMES_MASTER_APIKEY"):
        os.environ[_var] = master_key


def _run_harness_bg(harness_name: str, log_file: Path | None,
                    timeout: int = 300, context: dict | None = None) -> None:
    """Harness를 백그라운드 스레드에서 Python API로 직접 실행.
    brain_watcher inotify 이벤트에 의해 자율 트리거됨.
    완료 후 kb_sync를 즉시 실행해 brain.db에 결과 인덱싱.
    """
    def _worker() -> None:
        try:
            import sys as _sys
            nova_src    = Path.home() / "nova"
            hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
            nova_home   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
            hermes_bin  = hermes_home / "bin"
            for p in (str(hermes_bin), str(nova_src)):
                if p not in _sys.path:
                    _sys.path.insert(0, p)

            from nova.core.config import load_config
            from nova.core.harness import HarnessLoader
            from nova.core.orchestrator import Orchestrator

            # BUG-APIKEY: *** 독립 프로세스 → Hermes nova_bridge가 주입하는
            # NOVA_LLM_API_KEY 등이 없음.
            # 우선순위: .env HERMES_MASTER_APIKEY → config.yaml model.api_key
            try:
                import yaml as _yaml
                _env_path = hermes_home / ".env"
                _master_key = ""
                # 1순위: .env HERMES_MASTER_APIKEY
                if _env_path.exists():
                    for _line in _env_path.read_text(errors="replace").splitlines():
                        _line = _line.strip()
                        if _line.startswith("HERMES_MASTER_APIKEY="):
                            _master_key = _line.split("=", 1)[1].strip()
                            break
                # 2순위: config.yaml model.api_key (폴백)
                if not _master_key:
                    _hcfg_path = hermes_home / "config.yaml"
                    if _hcfg_path.exists():
                        _hcfg = _yaml.safe_load(_hcfg_path.read_text()) or {}
                        _master_key = _hcfg.get("model", {}).get("api_key", "")
                if _master_key:
                    _apply_master_key_llm_defaults(_master_key, nova_yaml_path=nova_home / "nova.yaml")
            except Exception:
                pass  # API key 주입 실패해도 계속 진행

            cfg = load_config(str(nova_home / "nova.yaml"))
            cfg.harnesses_dir = str(Path(cfg.harnesses_dir).expanduser())
            cfg.workspace     = str(Path(cfg.workspace).expanduser())

            loader  = HarnessLoader(cfg.harnesses_dir)
            harness = loader.load(harness_name)
            orch    = Orchestrator(cfg)
            # BUG-HARNESS-TOPIC 수정: context 파라미터로 topic 주입 지원
            run_ctx = context if context else {}
            ok      = orch.run(harness, context=run_ctx, resume=False)

            if ok:
                _log(f"  [harness:{harness_name}] OK — report 생성 완료", log_file)
                # harness 완료 즉시 kb_sync — workspace → brain.db 인덱싱 (BUG-A2 수정)
                # WARN-4 수정: /usr/bin/python3 우선 (sqlite_vec 위치 안전)
                _kb_py = None
                for _try_py in ("/usr/bin/python3", "/usr/local/bin/python3", _sys.executable):
                    try:
                        import subprocess as _sp_chk
                        if _sp_chk.run([_try_py, "-c", "import sqlite_vec"], capture_output=True).returncode == 0:
                            _kb_py = _try_py; break
                    except Exception:
                        pass
                if not _kb_py:
                    _kb_py = _sys.executable
                sync_r = subprocess.run(
                    [_kb_py, str(hermes_bin / "nova_kb_sync.py"), "--no-embed"],
                    env={**os.environ,
                         "HERMES_HOME": str(hermes_home),
                         "NOVA_HOME":   str(nova_home),
                         "PYTHONPATH":  str(hermes_bin) + ":" + str(nova_src)},
                    capture_output=True, text=True, timeout=120,
                )
                if sync_r.returncode == 0:
                    _log(f"  [harness:{harness_name}] kb_sync 완료 → brain.db 갱신", log_file)
                    # kb_harvest: harness report.md → KB projects/nova-harness-log.md 갱신
                    harvest_script = hermes_home / "bin" / "nova_kb_harvest.py"
                    if harvest_script.exists():
                        _run_bg([_sys.executable, str(harvest_script)], "kb_harvest", log_file, timeout=30)
                    # kb_sync 성공 후 → claim extract (harness report.md → takes 자동 생성)
                    claim_script = hermes_home / "bin" / "nova_kb_claim_extract.py"
                    if claim_script.exists():
                        subprocess.run(
                            [_sys.executable, str(claim_script)],
                            env={**os.environ, "HERMES_HOME": str(hermes_home), "NOVA_HOME": str(nova_home)},
                            capture_output=True, text=True, timeout=60,
                        )
                        _log(f"  [harness:{harness_name}] claim_extract 완료 → takes 자동 생성", log_file)
                    # kb_sync 성공 → takes_link 즉시 실행 (coverage 개선)
                    import pathlib as _pathlib
                    takes_link_script = _pathlib.Path.home() / ".nova/engines/takes_link.py"
                    if takes_link_script.exists():
                        subprocess.run(
                            [_sys.executable, str(takes_link_script)],
                            env={**os.environ, "NOVA_HOME": str(nova_home)},
                            capture_output=True, text=True, timeout=30,
                        )
                        # 이중실행 방지: state 파일에 last_takes_link 직접 갱신
                        try:
                            import json as _json, time as _time
                            _sf = nova_home / "logs" / "brain_watcher_state.json"
                            if _sf.exists():
                                _st = _json.loads(_sf.read_text())
                                _st["last_takes_link"] = _time.time()
                                _sf.write_text(_json.dumps(_st, indent=2))
                        except Exception:
                            pass
                        _log(f"  [harness:{harness_name}] takes_link 완료 → coverage 개선", log_file)
                else:
                    _log(f"  [harness:{harness_name}] kb_sync 실패 rc={sync_r.returncode} {(sync_r.stderr or sync_r.stdout)[:100]}", log_file)
            else:
                _log(f"  [harness:{harness_name}] FAIL — Orchestrator 오류", log_file)
        except Exception as e:
            _log(f"  [harness:{harness_name}] EXCEPTION {e}", log_file)

    threading.Thread(target=_worker, daemon=False).start()  # daemon=False: 재시작 시 harness 완료 보장


# ── reaction logic ────────────────────────────────────────────────────────────

REACT = {
    "takes_for_dream":       100,   # +N takes → DreamCycle
    "takes_for_synthesize":   15,   # +N takes → synthesize
    "takes_for_learn":         5,   # +N takes → learn_engine
    "takes_for_harness":      20,   # +N takes → research harness (자율 지식 생산)
    "orphan_max":              3,   # orphan ≥ N → fix_orphan
    "health_critical":        85.0, # health < N → DreamCycle (87 안정권 → 만성 낭비 방지)
    "chain_min_s":            30,   # 30초 cooldown — 에이전트 실행 중 과도한 spawn 방지
    "synthesize_min_s":      300,
    "dream_min_s":          7200,   # 2 h
    "learn_min_s":          1800,   # 30 min
    "harness_min_s":        1200,   # 20 min (대화 맥락 유지 충분)
    "crosslink_min_s":     21600,   # 6 h
    "takes_wiki_min_s":    43200,   # 12 h
    "stale_wiki_min_s":    86400,   # 24 h
    "memory_check_min_s":   7200,   # 2 h (30 min → 2 h: 과잉 slim 순환 방지)
    "memory_slim_threshold":  90,   # % — 90%(1980자) 이상일 때만 slim (85 → 90: slim 과열 방지)
    "memory_limit_chars":   2_200,  # BUG-W2b: Hermes 실제 한계 2200자와 일치
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
    # BUG-HARNESS-RESET 수정: brain_prev 기반 new_takes는 재시작 시마다 초기화됨.
    # state["takes_at_last_harness"]를 영속화해 재시작 내성(restart-tolerant) 누적 카운팅.
    # harness 전용 new_takes는 state에서, 그 외 엔진용은 in-memory brain_prev 사용.
    new_takes = brain_now["takes"] - brain_prev.get("takes", brain_now["takes"])
    harness_base = state.get("takes_at_last_harness", brain_now["takes"])
    new_takes_for_harness = brain_now["takes"] - harness_base
    # BUG-DREAM-RESET 수정 (2026-07-30): dream도 재시작 내성 누적 카운팅
    # in-memory new_takes는 한 루프(1초)에 100개 불가 → 영구 미트리거
    # harness와 동일하게 state["takes_at_last_dream"] 기반 delta 사용
    dream_base = state.get("takes_at_last_dream", brain_now["takes"])
    new_takes_for_dream = brain_now["takes"] - dream_base

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

    # orphan takes 자동연결 (30분마다, takes_link 엔진 있을 때)
    # brain_watcher orphan은 pages 기준이고 takes orphan(page_id=NULL)은 별도 처리 필요
    if "takes_link" in engines and _can_act(state, "takes_link", 1800):
        _log("  → takes_link (orphan takes 연결)", log_file)
        _run_bg(engines["takes_link"], "takes_link", log_file, timeout=60)
        state["last_takes_link"] = time.time()
        acted.append("takes_link")

    # Kanban done → chain_engine
    if kanban_now and kanban_prev:
        new_done = kanban_now["done"] - kanban_prev.get("done", kanban_now["done"])
        # ★ chain_engine 트리거 조건 (3가지):
        # 1) new_done > 0: done 증가 → 정상 트리거
        # 2) has_ready: active(running/todo/ready) 있고 done도 있음 → 루프 진행 중 재트리거
        # 3) done_only_changed: active=0이지만 done이 변했음 → 마지막 에이전트 완료 후 체인 처리 필요
        #    (nova-investigate done → nova-dev 재생성 등 체인 로직이 아직 남아있을 수 있음)
        has_ready = kanban_now.get("active", 0) > 0 and kanban_now.get("done", 0) > 0
        kanban_state_changed = (kanban_now != kanban_prev)
        # active=0이고 done이 있을 때도 상태 변화 시 체인 실행 (마지막 에이전트 완료 처리)
        done_only_changed = (kanban_now.get("active", 0) == 0 and
                             kanban_now.get("done", 0) > 0 and
                             kanban_state_changed)
        chain_trigger = (new_done > 0) or (has_ready and kanban_state_changed) or done_only_changed
        if chain_trigger and _can_act(state, "chain", R["chain_min_s"]):
            reason = (f"done +{new_done}" if new_done > 0 else
                      f"ready tasks (active={kanban_now.get('active')})" if has_ready else
                      f"done-only state change (done={kanban_now.get('done')})")
            _log(f"  kanban {reason} → chain_engine", log_file)
            if "chain" in engines:
                # timeout=3600: chain_engine이 내부적으로 orchestrator --wait를 호출하므로
                # harness 실행 시간(최대 ~10분 × 에이전트 수)을 수용해야 함
                _run_bg(engines["chain"], "chain_engine", log_file, timeout=3600)
            state["last_chain"] = time.time()
            acted.append("chain_engine")

    # Takes reactions (tiered)
    # BUG-DREAM-RESET: new_takes_for_dream (영속화) 사용 — 재시작 내성
    if new_takes_for_dream >= R["takes_for_dream"] and _can_act(state, "dream", R["dream_min_s"]):
        _log(f"  takes +{new_takes_for_dream} (persistent) → DreamCycle", log_file)
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

    # harness는 elif 체인과 독립 — synthesize/learn과 무관하게 별도 판단
    # new_takes_for_harness: state 영속화 기반 누적 (재시작 내성)
    # takes 20개 이상 누적(synthesize 임계값 15 초과) = 지속적 대화 →
    # research harness로 심화 탐구 → workspace/report.md → kb_sync → brain.db
    if new_takes_for_harness >= R["takes_for_harness"] and _can_act(state, "harness", R["harness_min_s"]):
        _log(f"  takes +{new_takes_for_harness} (누적) → harness 라우팅 (자율 지식 생산)", log_file)
        # BUG-HARNESS-TOPIC 수정: brain 상태 기반 topic 자동 생성 ({{topic}} 플레이스홀더 미치환 방지)
        harness_topic = (
            f"NOVA KB 자율 탐구 — 최근 {new_takes_for_harness}개 대화 인사이트 기반 핵심 주제 분석. "
            f"brain.db: takes={brain_now['takes']}, pages={brain_now.get('total_pages', '?')}, "
            f"health={brain_now['health']}"
        )
        # Phase 4: InterruptRouter 경유 도메인별 harness 라우팅
        harness_name = "research"  # 기본 폴백
        try:
            import sys as _sys
            _nova_src = str(Path.home() / "nova")
            if _nova_src not in _sys.path:
                _sys.path.insert(0, _nova_src)
            from nova.kernel.interrupt import InterruptRouter
            from nova.kernel.memory import MemoryLayer

            # brain_db 경로: _react()는 nova_home 인자를 받지 않으므로 환경변수에서 재구성
            _ir_nova_home = Path(os.environ.get("NOVA_HOME", str(Path.home() / ".nova"))).expanduser()
            _ir_brain_db  = str(_ir_nova_home / "brain.db")

            _layer = MemoryLayer(brain_db=_ir_brain_db)
            _recent_takes = _layer.get_takes(tier="hot",  limit=20)
            _warm_takes   = _layer.get_takes(tier="warm", limit=30)
            _router       = InterruptRouter()  # domain_routing.yaml 자동 로드
            _interrupts   = _router.classify(_warm_takes + _recent_takes)  # warm(old) + hot(new)

            if _interrupts:
                _intr       = _interrupts[0]  # 최우선 1개
                harness_name = _router.route(_intr)
                _log(
                    f"  interrupt: {_intr.kind.value} domain={_intr.domain} "
                    f"conf={_intr.confidence:.2f} → {harness_name}",
                    log_file,
                )
            else:
                _log("  interrupt: 도메인 미매칭 → research 폴백", log_file)
        except Exception as _ie:
            _log(f"  interrupt 실패(폴백 research): {_ie}", log_file)

        _run_harness_bg(harness_name, log_file, context={"topic": harness_topic})
        state["last_harness"] = time.time()
        state["takes_at_last_harness"] = brain_now["takes"]  # BUG-HARNESS-RESET: 영속화
        acted.append(f"harness_{harness_name}")

    # ── Phase 2: nova_events 'spawn' 폴링 → 실제 harness 실행 (I-3 해결) ──────
    # KernelAPI.spawn()은 nova_events에 INSERT만 함. 이 블록이 실제 실행을 담당.
    # nova_events 컬럼: id, event_type, severity, title, detail, source, created_at, is_read, source_agent
    # is_read=0 인 spawn 이벤트를 폴링하고 harness를 실행한 뒤 is_read=1로 마킹.
    try:
        import sqlite3 as _sqlite3
        # _react()는 nova_home 인자를 받지 않으므로 환경변수에서 재구성
        _ne_nova_home = Path(os.environ.get("NOVA_HOME", str(Path.home() / ".nova"))).expanduser()
        _ne_db_path = _ne_nova_home / "brain.db"
        _ne_conn = _sqlite3.connect(str(_ne_db_path), timeout=3)
        _ne_conn.execute("PRAGMA busy_timeout=2000")
        _spawn_rows = _ne_conn.execute(
            "SELECT id, title, detail, source_agent FROM nova_events "
            "WHERE event_type='spawn' AND is_read=0 "
            "ORDER BY created_at ASC LIMIT 5"
        ).fetchall()
        for _ne_id, _ne_title, _ne_detail, _ne_source in _spawn_rows:
            # title 형식: "spawn:<harness_name>" 또는 harness명 직접
            _harness_name = _ne_title or ""
            if _harness_name.startswith("spawn:"):
                _harness_name = _harness_name.split(":", 1)[1]
            _harness_name = _harness_name.strip() or "research"
            _ne_topic = (
                str(_ne_detail or "").strip()[:120]
                or f"nova_events spawn: {_harness_name}"
            )
            if _can_act(state, f"spawn_{_harness_name}", R.get("harness_min_s", 1200)):
                _log(
                    f"  [nova_events] spawn({_harness_name}) from {_ne_source} — topic: {_ne_topic[:60]}",
                    log_file,
                )
                _run_harness_bg(_harness_name, log_file, context={"topic": _ne_topic})
                state[f"last_spawn_{_harness_name}"] = time.time()
                acted.append(f"spawn_{_harness_name}")
            else:
                _log(
                    f"  [nova_events] spawn({_harness_name}) 쿨다운 중 — 스킵",
                    log_file,
                )
            # 처리 완료 마킹 (is_read=1)
            _ne_conn.execute(
                "UPDATE nova_events SET is_read=1 WHERE id=?",
                (_ne_id,),
            )
        if _spawn_rows:
            _ne_conn.commit()
        _ne_conn.close()
    except Exception as _ne_err:
        _log(f"  [nova_events] 폴링 실패 (무시): {_ne_err}", log_file)

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
                # BUG-W6 수정: state 기록은 실제 실행 시에만 (if 블록 안으로 이동)
                state["last_memory_slim"] = time.time()
                acted.append(f"memory_slim_{snap['pct']}pct")
            else:
                # 엔진 없을 때 경고 로그 (허위 acted 기록 방지)
                _log(f"  WARN: memory_slim 엔진 없음 — 메모리 {snap['pct']}% 위험, engines/ 확인 필요", log_file)

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
    # BUG-CRITICAL-2 수정: kanban은 ~/.nova가 아닌 ~/.hermes에 위치
    # nova_home = ~/.nova, hermes_home = ~/.hermes
    # 실제 kanban DB: ~/.hermes/kanban/boards/<board>/kanban.db
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()

    # [BUG-FIX] watcher 시작 즉시 .env / config.yaml에서 최신 키 강제 주입
    # os.environ.setdefault()는 셸에서 구키가 export된 경우 무시됨 → 강제 덮어쓰기 필요
    try:
        import yaml as _yaml
        _env_path = hermes_home / ".env"
        _master_key = ""
        if _env_path.exists():
            for _line in _env_path.read_text(errors="replace").splitlines():
                _line = _line.strip()
                if _line.startswith("HERMES_MASTER_APIKEY="):
                    _master_key = _line.split("=", 1)[1].strip()
                    break
        if not _master_key:
            _hcfg = _yaml.safe_load((hermes_home / "config.yaml").read_text()) or {}
            _master_key = _hcfg.get("model", {}).get("api_key", "")
        if _master_key:
            for _var in ("HERMES_MASTER_APIKEY", "NOVA_LLM_API_KEY", "HMG_API_KEY",
                         "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY",
                         "NOVA_CODEX_API_KEY", "NOVA_IMAGE_GEN_API_KEY"):
                os.environ[_var] = _master_key
    except Exception:
        pass
    kanban_root = hermes_home / "kanban" / "boards"
    # fallback: nova_home에도 kanban이 있으면 함께 감시
    nova_kanban_root = nova_home / "kanban" / "boards"
    state_file = nova_home / "logs" / "brain_watcher_state.json"
    log_file = nova_home / "logs" / "brain_watcher.log"
    memory_md = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "memories" / "MEMORY.md"
    # BUG-E3 수정: 에이전트들이 읽는 MEMORY.md와 동일한 경로 사용
    # (기존: nova_home/"memory.md" → slim 트리거 불일치)

    (nova_home / "logs").mkdir(parents=True, exist_ok=True)

    # Resolve kanban board dirs (hermes + nova 두 경로 모두 감시)
    def _kanban_dirs() -> list[Path]:
        dirs: list[Path] = []
        for root in (kanban_root, nova_kanban_root):
            if root.exists():
                dirs.extend(d for d in root.iterdir()
                            if d.is_dir() and (d / "kanban.db").exists())
        return dirs

    # Engine defaults (override with engines= param)
    engines_dir = nova_home / "engines"
    default_engines: dict[str, list[str]] = {
        "dream":       [sys.executable, str(engines_dir / "dream.py")],
        "synthesize":  [sys.executable, str(engines_dir / "synthesize.py")],
        "learn":       [sys.executable, str(engines_dir / "learn.py")],
        "chain":       [sys.executable, str(engines_dir / "chain.py")],
        "fix_orphan":  [sys.executable, str(engines_dir / "fix_orphan.py")],
        "memory_slim": [sys.executable, str(engines_dir / "memory_slim.py")],
        "takes_link":  [sys.executable, str(engines_dir / "takes_link.py")],  # orphan takes → pages 자동연결
        "kb_harvest":  [sys.executable, str(hermes_home / "bin" / "nova_kb_harvest.py")],  # harness report → KB
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
    kanban_restart_prefix = str(kanban_root) if kanban_root.exists() else (
        str(nova_kanban_root) if nova_kanban_root.exists() else None
    )

    brain_prev = _snap_brain(brain_db) or {}
    kanban_prev = _snap_kanban(board_dirs)

    _log("started — inotify event-driven, non-recursive", log_file)
    _log(f"  brain_db: {brain_db}", log_file)
    _log(f"  watch_dirs: {watch_dirs}", log_file)
    if active_engines:
        _log(f"  engines: {list(active_engines.keys())}", log_file)

    # ★ 시작 시 즉시 체인 트리거: done이 있고 active가 없으면 미처리 체인 태스크가 있을 수 있음
    # (watcher 재시작 직후 kanban_prev==kanban_now이므로 이벤트 루프에서 절대 트리거 안 됨)
    if kanban_prev and kanban_prev.get("done", 0) > 0 and kanban_prev.get("active", 0) == 0:
        _log(f"  [STARTUP] done={kanban_prev['done']} active=0 → chain_engine 즉시 트리거", log_file)
        if "chain" in active_engines and _can_act(state, "chain", 0):  # startup은 cooldown 무시
            _run_bg(active_engines["chain"], "chain_engine", log_file, timeout=3600)
            state["last_chain"] = time.time()

    _last_cron_tick = 0.0
    _CRON_INTERVAL  = 3600  # 1시간마다 cron_tick 실행

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
                        # BUG-INOTIFY-LEAK 수정: break 전에 현재 inotify proc 명시적 종료
                        # 이전에는 finally에서 terminate했으나 break 시 finally가 실행되지 않아
                        # 재시작할 때마다 inotifywait 좀비 프로세스가 1개씩 누적됨
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except Exception:
                            try:
                                proc.kill()
                                proc.wait(timeout=1)
                            except Exception:
                                pass
                        break
                    # Ignore ISDIR events from other directories (backups, cache…)
                    continue

                # Only react to actual DB file changes
                if filename not in _DB_FILENAMES:
                    continue

                # BUG-CPU-SPIN: 마라톤 중 kanban.db가 초당 수십 번 업데이트됨
                # 이벤트를 최소 0.5초 간격으로 처리 — CPU 절감 + 상태 정확도 유지
                _now = time.time()
                if _now - state.get("_last_event_ts", 0) < 0.5:
                    continue
                state["_last_event_ts"] = _now

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
            # ── cron_tick: 1시간마다 자율 harness 트리거 (사용자 비활성 시에도 지식 성장) ──
            if _CRON_ENGINE_AVAILABLE and time.time() - _last_cron_tick >= _CRON_INTERVAL:
                _last_cron_tick = time.time()
                try:
                    _cron_tick(
                        brain_db=str(nova_home / "brain.db"),
                        run_harness_fn=lambda name: (
                            # _run_harness_bg는 None 반환 — 백그라운드 제출 자체를 성공으로 처리
                            # 실제 성공 여부는 cron_engine 쿨다운 + brain_watcher 로그로 추적
                            _run_harness_bg(name, log_file, timeout=300) is not None or True
                        ),
                        log_fn=lambda msg: _log(msg, log_file),
                    )
                except Exception as _ce:
                    _log(f"  [cron_engine] 예외: {_ce}", log_file)


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
