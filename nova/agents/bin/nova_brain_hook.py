#!/usr/bin/env python3
"""
nova_brain_hook.py — kb_pipeline.py 완료 후 nova_brain.db 자동 연동 훅
kb_watcher가 파일 변경 감지 → kb_pipeline 실행 → 이 훅 실행

사용 (kb_pipeline.py의 Layer 4로 추가):
  python3 nova_brain_hook.py <kb_file_path>
  python3 nova_brain_hook.py --full-sync
"""
import os
import sys
import subprocess
from pathlib import Path

NOVA_BRAIN = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin/nova_brain.py"
KB_ROOT    = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"


def index_file(kb_rel_path: str):
    """단일 파일 nova_brain 인덱싱"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("nova_brain", str(NOVA_BRAIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    brain = mod.NovaBrain()
    ok = brain.index_kb_file(kb_rel_path, embed=True)  # 벡터도 즉시 생성
    brain.close()
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: nova_brain_hook.py <kb_path> | --full-sync")
        sys.exit(1)

    if sys.argv[1] == "--full-sync":
        # 전체 재인덱싱
        result = subprocess.run(
            [sys.executable, str(NOVA_BRAIN), "index-all"],
            capture_output=True, text=True
        )
        print(result.stdout)
    else:
        # 단일 파일
        abs_path = Path(sys.argv[1])
        if abs_path.is_absolute():
            try:
                rel = str(abs_path.relative_to(KB_ROOT))
            except ValueError:
                rel = str(abs_path)
        else:
            rel = sys.argv[1]

        ok = index_file(rel)
        print(f"[nova_brain] {'OK' if ok else 'SKIP'}: {rel}")
