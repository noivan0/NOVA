#!/bin/bash
# NOVA 자율 디스패처 v3.0
# 역할:
#   1. Kanban ready 태스크 → 각 보드 dispatch
#   2. nova_chain_engine.py v3.0 실행 (DoD 게이트 + 역방향 점프)
# 크론: */5 * * * *

set -euo pipefail

# ① 보드별 Kanban dispatch (nova_boards.json에서 동적 로드)
BOARDS_JSON="${HERMES_HOME:-$HOME/.hermes}/kanban/nova_boards.json"
if [ -f "$BOARDS_JSON" ]; then
    BOARDS=$(python3 -c "import json; d=json.load(open('$BOARDS_JSON')); print(' '.join(d.get('boards', [])))")
else
    echo "[nova_app_dispatch] ERROR: nova_boards.json 없음 — dispatch 중단 (경로: $BOARDS_JSON)" >&2
    exit 1
fi

for BOARD in $BOARDS; do
    hermes kanban --board "$BOARD" dispatch --max 2 2>/dev/null || true
done

# ② DoD 게이트 + 역방향 점프 체인 엔진 실행
python3 ${HERMES_HOME:-$HOME/.hermes}/scripts/nova_chain_engine.py 2>/dev/null || true
