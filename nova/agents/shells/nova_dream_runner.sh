#!/bin/bash
# nova_dream_runner.sh — NOVA Dream Cycle no_agent 실행 (silent watchdog)
# 결과: dream 보고서 경로 + 헬스 점수 출력
# flock 추가: 동시 실행 방지 (R16 정밀감사)

LOCK_FILE=/tmp/nova_dream.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[nova-dream] SKIP (already running)"
    exit 77  # BUG-H1 fix: 77=skip, 0=success, 명확히 구분
fi

RESULT=$(timeout 580 python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_dream.py 2>&1 | tail -5)
STATUS=$?

if [ $STATUS -eq 0 ]; then
    HEALTH=$(echo "$RESULT" | grep -oP '헬스:\s*\K[0-9.]+' || echo "?")
    DATE=$(date +%Y-%m-%d)
    echo "[nova-dream] ${DATE} 완료 — 헬스: ${HEALTH} | ${RESULT}"
elif [ $STATUS -eq 124 ]; then
    echo "[nova-dream] TIMEOUT (580s 초과)"
else
    echo "[nova-dream] ERROR: exit=$STATUS | $RESULT"
fi
