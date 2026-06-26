#!/bin/bash
# NOVA Evaluator 일일 측정 스크립트
# 결과를 stdout으로 출력 → 에이전트 프롬프트에 주입됨
# [gbrain Proactive Synthesis 강화] 당일 신규 패턴 + takes 추이 추가

DATE=$(date +%Y-%m-%d)
PREV_DATE=$(ls ${HERMES_HOME:-$HOME/.hermes}/kb/agents/nova-evaluator/*.md 2>/dev/null | sort | tail -1 | xargs basename 2>/dev/null | sed 's/.md//')

echo "=== NOVA SYSTEM METRICS: $DATE ==="
echo "Previous measurement: ${PREV_DATE:-none}"
echo ""

echo "--- KRAYT ---"
RUN_COUNT=$(grep -r "async def _run_" ${HERMES_HOME:-$HOME/.hermes}/projects/ai-qa-startup/ 2>/dev/null | wc -l | tr -d ' ')
PY_COUNT=$(find ${HERMES_HOME:-$HOME/.hermes}/projects/ai-qa-startup/src -name '*.py' 2>/dev/null | wc -l | tr -d ' ')
echo "run-methods: $RUN_COUNT"
echo "python-files: $PY_COUNT"

echo ""
echo "--- Death Mode ---"
FREQTRADE_PROCS=$(ps aux | grep freqtrade | grep -v grep | wc -l | tr -d ' ')
echo "freqtrade-processes: $FREQTRADE_PROCS"

echo ""
echo "--- Kanban ---"
hermes kanban assignees 2>/dev/null | tail -8 || echo "unavailable"

echo ""
echo "--- KB System ---"
python3 ${HERMES_HOME:-$HOME/.hermes}/bin/kb_pipeline.py --stats 2>/dev/null || echo "unavailable"
AGENT_KB=$(ls ${HERMES_HOME:-$HOME/.hermes}/kb/agents/*/*.md 2>/dev/null | wc -l | tr -d ' ')
echo "agent-kb-files: $AGENT_KB"

echo ""
echo "--- [gbrain] nova_brain 건강 지표 ---"
python3 << 'PYEOF'
import sqlite3, json
from datetime import datetime, timezone, timedelta

DB = '${HERMES_HOME:-$HOME/.hermes}/nova_brain.db'
try:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 전체 takes 수 + 오늘 신규
    today = datetime.now(timezone.utc).date().isoformat()
    cur.execute("SELECT COUNT(*) FROM takes")
    total_takes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM takes WHERE created_at >= ?", (today,))
    new_takes_today = cur.fetchone()[0]
    print(f"takes-total: {total_takes}")
    print(f"takes-new-today: {new_takes_today}")

    # 최신 brain_health
    cur.execute("SELECT score_overall, score_coverage, score_depth, total_pages, open_contradictions FROM brain_health ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        print(f"brain-health-overall: {row[0]}")
        print(f"brain-health-coverage: {row[1]}")
        print(f"brain-health-depth: {row[2]}")
        print(f"brain-pages: {row[3]}")
        print(f"brain-contradictions-open: {row[4]}")

    # [gbrain Proactive Synthesis] 오늘 신규 KB 페이지 중 takes 미연결 = 학습 누락 후보
    cur.execute("SELECT COUNT(*) FROM pages WHERE created_at >= ? AND id NOT IN (SELECT DISTINCT page_id FROM takes WHERE page_id IS NOT NULL)", (today,))
    untaken_today = cur.fetchone()[0]
    print(f"untaken-pages-today: {untaken_today}  # takes 미기록 — nova-learn 대상")

    # agent_activity (최근 24h 활성 에이전트)
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur.execute("SELECT agent_name, COUNT(*) FROM agent_activity WHERE timestamp >= ? GROUP BY agent_name ORDER BY COUNT(*) DESC LIMIT 5", (yesterday,))
    rows = cur.fetchall()
    print(f"active-agents-24h: {[r[0] for r in rows]}")

    conn.close()
except Exception as e:
    print(f"nova_brain-error: {e}")
PYEOF

echo ""
echo "=== END METRICS ==="
