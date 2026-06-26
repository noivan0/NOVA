#!/bin/bash
# nova_brain_synthesize_runner.sh — NOVA Synthesize no_agent 실행 (silent watchdog)
# 결과: 새 KB 파일 수 + 헬스 점수 출력

DATE=$(date +%Y-%m-%d)
RESULT=$(timeout 600 python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_synthesize.py --auto --days 1 2>&1)
STATUS=$?

if [ $STATUS -eq 0 ]; then
    KB_COUNT=$(echo "$RESULT" | grep -oP '새 KB[:\s]+\K[0-9]+' || echo "?")
    echo "[nova-synthesize] ${DATE} 완료 | KB신규: ${KB_COUNT} | $(echo "$RESULT" | tail -3)"
elif [ $STATUS -eq 124 ]; then
    echo "[nova-synthesize] TIMEOUT (600s 초과)"
else
    echo "[nova-synthesize] ERROR: exit=$STATUS | $(echo "$RESULT" | tail -5)"
fi
