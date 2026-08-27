#!/usr/bin/env python3
"""
nova_kb_on_change.py — KB 파일 변경 감지 즉시 동기화 트리거
사용법:
  python3 nova_kb_on_change.py          # 변경 감지 후 즉시 sync
  python3 nova_kb_on_change.py --force  # 무조건 즉시 sync

Hermes가 KB에 기록할 때 자동 호출 or 수동 호출.
brain.db 인덱싱 즉시 반영 → 다음 kb_unified_search부터 반영됨.
"""
import subprocess, sys, os, time, hashlib
from pathlib import Path

NOVA_HOME   = os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))
HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
KB_PATH     = Path(HERMES_HOME) / "kb"
STAMP_FILE  = Path(NOVA_HOME) / "logs" / "kb_last_sync.stamp"

ENV = {**os.environ,
       "NOVA_HOME":        NOVA_HOME,
       "HERMES_HOME":      HERMES_HOME,
       "NOVA_LLM_PROVIDER":os.environ.get("NOVA_LLM_PROVIDER", "anthropic"),
       "NOVA_LLM_BASE_URL":os.environ.get("NOVA_LLM_BASE_URL", ""),
       "NOVA_LLM_MODEL":   os.environ.get("NOVA_LLM_MODEL", "claude-sonnet-4-5")}

def kb_fingerprint():
    """KB 디렉토리의 파일 mtime 합산으로 변경 감지"""
    h = hashlib.md5()
    for f in sorted(KB_PATH.rglob("*.md")):
        h.update(str(f.stat().st_mtime).encode())
    return h.hexdigest()

def run_sync():
    steps = [
        ("kb_harvest",     [sys.executable, f"{HERMES_HOME}/bin/nova_kb_harvest.py"]),
        ("kb_pipeline",    [sys.executable, f"{NOVA_HOME}/engines/kb_pipeline.py"]),
        ("takes_link",     [sys.executable, f"{NOVA_HOME}/engines/takes_link.py"]),
        ("sync_to_hermes", [sys.executable, f"{NOVA_HOME}/sync_to_hermes.py"]),
    ]
    results = []
    for name, cmd in steps:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=ENV)
        status = "OK" if r.returncode == 0 else f"FAIL rc={r.returncode}"
        out = (r.stdout + r.stderr).strip()
        results.append(f"[{name}] {status} — {out[:100]}")
        print(results[-1])
    return results

def main():
    force = "--force" in sys.argv

    current_fp = kb_fingerprint()

    if not force:
        prev_fp = STAMP_FILE.read_text().strip() if STAMP_FILE.exists() else ""
        if current_fp == prev_fp:
            print("[nova_kb_on_change] KB 변경 없음 — sync 스킵")
            return

    print(f"[nova_kb_on_change] KB 변경 감지 — 즉시 sync 시작 ({time.strftime('%H:%M:%S')})")
    run_sync()

    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAMP_FILE.write_text(current_fp)

    # kb_graph_sync: wikilinks → knowledge_graph_edges 자동 파싱
    try:
        import subprocess as _sub
        _gs = subprocess.run(
            [sys.executable, str(Path(HERMES_HOME) / 'bin' / 'kb_graph_sync.py')],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'HERMES_HOME': HERMES_HOME, 'NOVA_HOME': NOVA_HOME}
        )
        if _gs.returncode == 0:
            print(_gs.stdout.strip())
        else:
            print(f'[kb_graph_sync] WARN: {_gs.stderr[:100]}')
    except Exception as _e:
        print(f'[kb_graph_sync] skip: {_e}')

    # failure_to_test: CHAIN_FAIL 이벤트 → 영구 테스트 자동 전환
    try:
        _ft = subprocess.run(
            [sys.executable, str(Path(HERMES_HOME) / 'bin' / 'nova_failure_to_test.py')],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'HERMES_HOME': HERMES_HOME, 'NOVA_HOME': NOVA_HOME}
        )
        if _ft.returncode == 0:
            print(_ft.stdout.strip())
        else:
            print(f'[failure_to_test] WARN: {_ft.stderr[:100]}')
    except Exception as _fe:
        print(f'[failure_to_test] skip: {_fe}')

    # kb_index_auto_update: 신규 KB 페이지 index.md 자동 등록 (Graph Engineering Step6)
    try:
        _iu = subprocess.run(
            [sys.executable, str(Path(HERMES_HOME) / 'bin' / 'kb_index_auto_update.py')],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'HERMES_HOME': HERMES_HOME, 'NOVA_HOME': NOVA_HOME}
        )
        if _iu.returncode == 0:
            print(_iu.stdout.strip())
        else:
            print(f'[kb_index_auto_update] WARN: {_iu.stderr[:100]}')
    except Exception as _ie:
        print(f'[kb_index_auto_update] skip: {_ie}')

    # nova_state_manager: brain.db → state.md compound memory 동기화
    try:
        _sm = subprocess.run(
            [sys.executable, str(Path(HERMES_HOME) / 'bin' / 'nova_state_manager.py'), 'sync'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'HERMES_HOME': HERMES_HOME, 'NOVA_HOME': NOVA_HOME}
        )
        if _sm.returncode == 0:
            print(_sm.stdout.strip())
    except Exception as _se:
        print(f'[state_manager] skip: {_se}')

    print(f"[nova_kb_on_change] 완료 ({time.strftime('%H:%M:%S')})")

if __name__ == "__main__":
    main()
