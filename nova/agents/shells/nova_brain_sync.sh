#!/bin/bash
# nova_brain_sync.sh — nova_brain.db 벡터 없는 청크 임베딩 (no_agent, silent)
# 신규 KB 파일이 인덱싱됐지만 벡터가 없는 경우 배치 생성
# 신규 벡터가 생성됐을 때만 stdout 출력 (silent watchdog 패턴)

# 1회 실행당 최대 20개씩 처리 (HMG API 30s × 20 = 최대 600s 이내)
RESULT=$(timeout 580 python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_embed.py --sync --batch 20 2>/dev/null | grep "임베딩 완료:")
COUNT=$(echo "$RESULT" | grep -oP '\d+(?=개)')

if [ -n "$COUNT" ] && [ "$COUNT" -gt 0 ]; then
    echo "[nova-brain-sync] 벡터 생성: $COUNT개 ($(date +%Y-%m-%d))"
fi
