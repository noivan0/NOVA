#!/usr/bin/env python3
"""
nova_kb_full_sync.sh 래퍼 — WSL에서 실행
1. KB MD → brain.db 인덱싱 (kb_pipeline)
2. brain.db → KB MD 동기화 (sync_to_hermes)
3. takes_link (orphan 연결)
"""
import subprocess, sys, os
from pathlib import Path

NOVA_HOME   = os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))
HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV = {**os.environ,
       "NOVA_HOME":   NOVA_HOME,
       "HERMES_HOME": HERMES_HOME,
       "NOVA_LLM_PROVIDER": os.environ.get("NOVA_LLM_PROVIDER", "hmg"),
       "NOVA_LLM_BASE_URL": os.environ.get("NOVA_LLM_BASE_URL", "https://h-chat-api.autoever.com/claude-code/v2"),
       "NOVA_LLM_MODEL": os.environ.get("NOVA_LLM_MODEL", "claude-sonnet-5")}

steps = [
    ("kb_harvest",     [sys.executable, f"{HERMES_HOME}/bin/nova_kb_harvest.py"]),
    ("kb_pipeline",    [sys.executable, f"{NOVA_HOME}/engines/kb_pipeline.py"]),
    ("takes_link",     [sys.executable, f"{NOVA_HOME}/engines/takes_link.py"]),
    ("sync_to_hermes", [sys.executable, f"{NOVA_HOME}/sync_to_hermes.py"]),
]

for name, cmd in steps:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=ENV)
    out = (r.stdout + r.stderr).strip()
    status = "OK" if r.returncode == 0 else f"FAIL rc={r.returncode}"
    print(f"[{name}] {status} — {out[:120]}")
