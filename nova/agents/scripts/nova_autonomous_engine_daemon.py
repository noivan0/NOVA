#!/usr/bin/env python3
"""
nova_autonomous_engine_daemon.py
— supervisor 상시 실행용 래퍼 —

30초마다 nova_autonomous_engine.py의 판단 루프를 실행.
supervisor가 이 프로세스를 죽으면 자동 재시작.
즉 진짜 daemon: 크론 없이 항상 살아있는 NOVA 자율신경계.
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import subprocess, time, os, sys
from pathlib import Path

ENGINE = f"{_HERMES_HOME}/scripts/nova_autonomous_engine.py"
LOG    = f"{_HERMES_HOME}/logs/nova-autonomous-engine.log"
INTERVAL = 300  # 초 (5분 — brain_watcher가 세밀한 처리 담당)

print(f"[nova-daemon] 시작 — {INTERVAL}초 간격으로 판단 루프 실행", flush=True)

Path(LOG).parent.mkdir(parents=True, exist_ok=True)
cycle = 0

while True:
    cycle += 1
    try:
        result = subprocess.run(
            [sys.executable, ENGINE],
            capture_output=True, text=True, timeout=720  # dream 최대 580s + 여유 (기존 120→720)
        )
        output = result.stdout.strip()
        if output and output != "[nova-daemon] SILENT":
            # 행동이 있으면 출력
            for line in output.split("\n"):
                if any(kw in line for kw in ["CRITICAL","HIGH","MEDIUM","AUDIT","SPRINT","실행된 액션","EVENT"]):
                    print(f"[nova-daemon] cycle={cycle} {line}", flush=True)
        if result.returncode != 0 and result.stderr:
            print(f"[nova-daemon] ERROR cycle={cycle}: {result.stderr[:200]}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[nova-daemon] TIMEOUT cycle={cycle}", flush=True)
    except Exception as e:
        print(f"[nova-daemon] EXCEPTION cycle={cycle}: {e}", flush=True)

    time.sleep(INTERVAL)
