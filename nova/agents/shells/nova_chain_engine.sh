#!/bin/bash
# ============================================================
# NOVA 하네스 체인 엔진 v1.0
# 목적: done 감지 → 즉시 next_agent 태스크 auto-spawn
# 크론: */5 * * * * (5분마다)
# 노이반 NOVA 근본 목적 — 자율 성장 + 하네스 체이닝
# ============================================================

set -euo pipefail
LOG=/tmp/nova_chain_$(date +%Y%m%d).log
BOARDS="saju-wellness mental-load senior-care"

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG"; }

# ============================================================
# 워크플로우 체인 정의 (NOVA 9단계 순서)
# done 에이전트 → 자동 생성할 next 에이전트
# ============================================================
declare -A CHAIN
CHAIN["nova-research"]="nova-strategy"
CHAIN["nova-strategy"]="nova-autoplan"
CHAIN["nova-validator"]="nova-autoplan"
CHAIN["nova-autoplan"]="nova-dev"
CHAIN["nova-dev"]="nova-review"
CHAIN["nova-review"]="nova-cso"
CHAIN["nova-cso"]="nova-qa"
CHAIN["nova-qa"]="nova-ship"
CHAIN["nova-ship"]="nova-checkpoint"
CHAIN["nova-checkpoint"]="nova-canary"
CHAIN["nova-canary"]="nova-health"
CHAIN["nova-health"]="nova-evaluator"
CHAIN["nova-evaluator"]="nova-retro"
CHAIN["nova-retro"]="nova-learn"
CHAIN["nova-learn"]="nova-document"
CHAIN["nova-document"]="nova-document-release"

# 병목 감지 임계값 (분)
BOTTLENECK_MINUTES=60

# ============================================================
# 보드별 체인 처리
# ============================================================
for BOARD in $BOARDS; do
    log "--- 보드[$BOARD] 체인 점검 ---"
    
    # kanban 보드 전환
    hermes kanban boards switch "$BOARD" > /dev/null 2>&1 || continue
    
    # JSON 형식으로 태스크 목록 수신
    TASKS_JSON=$(hermes kanban list --json 2>/dev/null) || continue
    
    # done 태스크 처리 → next 에이전트 자동 생성
    echo "$TASKS_JSON" | python3 - <<'PYEOF'
import json, sys, os, subprocess, time
from datetime import datetime, timezone

tasks = json.load(sys.stdin)
board = os.environ.get("CURRENT_BOARD", "unknown")
log_file = f"/tmp/nova_chain_{datetime.now().strftime('%Y%m%d')}.log"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a") as f:
        f.write(line + "
")

# CHAIN 맵 로드 (bash에서 env로 전달 불가 → 재정의)
CHAIN = {
    "nova-research": "nova-strategy",
    "nova-strategy": "nova-autoplan",
    "nova-validator": "nova-autoplan",
    "nova-autoplan": "nova-dev",
    "nova-dev": "nova-review",
    "nova-review": "nova-cso",
    "nova-cso": "nova-qa",
    "nova-qa": "nova-ship",
    "nova-ship": "nova-checkpoint",
    "nova-checkpoint": "nova-canary",
    "nova-canary": "nova-health",
    "nova-health": "nova-evaluator",
    "nova-evaluator": "nova-retro",
    "nova-retro": "nova-learn",
    "nova-learn": "nova-document",
    "nova-document": "nova-document-release",
}

# 현재 보드 파악
result = subprocess.run(["hermes", "kanban", "boards"], capture_output=True, text=True)
current_board = "unknown"
for line in result.stdout.split("
"):
    if "●" in line:
        parts = line.split()
        if len(parts) > 1:
            current_board = parts[1]
            break

now = time.time()
done_tasks = [t for t in tasks if t.get("status") == "done"]
running_tasks = [t for t in tasks if t.get("status") == "running"]
blocked_tasks = [t for t in tasks if t.get("status") == "blocked"]
assignees_with_tasks = {t.get("assignee") for t in tasks if t.get("status") not in ("done", "archived")}

spawned = 0

for task in done_tasks:
    agent = task.get("assignee", "")
    task_id = task.get("id", "")
    title = task.get("title", "")
    completed_at = task.get("completed_at")
    
    next_agent = CHAIN.get(agent)
    if not next_agent:
        continue
    
    # 이미 next_agent 태스크가 존재하면 스킵 (중복 방지)
    if next_agent in assignees_with_tasks:
        log(f"  [SKIP] {agent}→{next_agent}: {next_agent} 이미 보드에 존재")
        continue
    
    # next 태스크 제목 자동 생성
    next_title = f"[Chain:{agent}→{next_agent}] {title}"
    next_body = (
        f"자율 체인 자동 생성 (nova_chain_engine)
"
        f"원본 태스크: {task_id} ({title})
"
        f"담당 에이전트: {next_agent}
"
        f"SOUL.md: ${HERMES_HOME:-$HOME/.hermes}/profiles/{next_agent}/SOUL.md
"
        f"harness.md: ${HERMES_HOME:-$HOME/.hermes}/profiles/{next_agent}/harness.md
"
        f"생성 시각: {datetime.now().isoformat()}"
    )
    
    cmd = [
        "hermes", "kanban",
        "--board", current_board,
        "create", next_title,
        "--assignee", next_agent,
        "--parent", task_id,
        "--body", next_body,
    ]
    
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        log(f"  [CHAIN] {agent} done → {next_agent} 태스크 생성 [{current_board}]: {next_title}")
        spawned += 1
    else:
        log(f"  [ERROR] 체인 생성 실패: {r.stderr.strip()[:100]}")

# 병목 감지 (60분+ running)
BOTTLENECK_SEC = 60 * 60
for task in running_tasks:
    started_at = task.get("started_at", 0)
    if started_at and (now - started_at) > BOTTLENECK_SEC:
        agent = task.get("assignee", "")
        task_id = task.get("id", "")
        title = task.get("title", "")
        elapsed_min = int((now - started_at) / 60)
        
        # nova-investigate 자동 생성 (이미 존재하지 않을 때만)
        if "nova-investigate" not in assignees_with_tasks:
            cmd = [
                "hermes", "kanban",
                "--board", current_board,
                "create", f"[병목감지] {agent} {elapsed_min}분 체류 조사",
                "--assignee", "nova-investigate",
                "--body", f"병목 감지:
- 에이전트: {agent}
- 태스크: {task_id} ({title})
- 체류시간: {elapsed_min}분

RCA(5Whys)로 원인 분석 후 보고",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                log(f"  [BOTTLENECK] {agent} {elapsed_min}분 → nova-investigate 자동 생성")

if spawned:
    log(f"  → {current_board}: {spawned}개 체인 태스크 생성 완료")
else:
    log(f"  → {current_board}: 신규 체인 없음 (정상)")
PYEOF
    
done
log "=== 체인 엔진 완료 ==="
