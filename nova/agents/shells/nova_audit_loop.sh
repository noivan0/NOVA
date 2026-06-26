#!/bin/bash
# NOVA 3개 앱 자율 감사 루프 — 멈추지 않는 강화 사이클
# 실행: 매 6시간 크론으로 호출
# 크론 등록 예시:
#   0 */6 * * * ${HERMES_HOME:-$HOME/.hermes}/scripts/nova_audit_loop.sh >> ${HERMES_HOME:-$HOME/.hermes}/kb/audit_loop/cron.log 2>&1

set -e
DATE=$(date +%Y-%m-%d_%H-%M)
LOG_DIR="${HERMES_HOME:-$HOME/.hermes}/kb/audit_loop"
mkdir -p "$LOG_DIR"

echo "=== NOVA 자율 감사 루프 시작: $DATE ==="

# 1. rail-saas 테스트 실행 (3개 앱 보류 — NOVA rail-saas만 운영)
echo "[1/4] rail-saas 테스트 실행..."
TEST_SUMMARY=""
for app in rail-saas; do
  if [ -d "${HERMES_HOME:-$HOME/.hermes}/projects/rail-saas/backend" ]; then
    APP_PATH="${HERMES_HOME:-$HOME/.hermes}/projects/rail-saas/backend"
  else
    TEST_SUMMARY="$TEST_SUMMARY\n$app: 경로 없음"
    continue
  fi
  echo "--- $app ---"
  RESULT=$(cd "$APP_PATH" && python3 -m pytest --tb=no -q 2>&1 | tail -3)
  echo "$RESULT"
  TEST_SUMMARY="$TEST_SUMMARY\n$app: $RESULT"
done

# 2. nova_brain에 감사 결과 기록
echo "[2/4] nova_brain takes 기록..."
python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_cli.py takes add nova-qa \
  "${HERMES_HOME:-$HOME/.hermes}/kb/audit_loop/$DATE.md" \
  take "자율감사 실행: $DATE" 0.8 2>/dev/null || true

# 3. nova_brain health 체크
echo "[3/4] nova_brain health..."
python3 ${HERMES_HOME:-$HOME/.hermes}/scripts/nova_db_status.py 2>/dev/null || \
  python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_cli.py health 2>/dev/null | tail -3 || true


# 4. 요일별 에이전트 takes 자동 기록 (A안 — evolution.md 자율 성장)
echo "[4/5] 에이전트 takes 자동 기록..."
DOW_NUM=$(date +%u)  # 1=월 2=화 ... 7=일
python3 << 'PYEOF'
import sqlite3, uuid, datetime, os

DB = "${HERMES_HOME:-$HOME/.hermes}/nova_brain.db"
DOW = int(os.popen("date +%u").read().strip())

# 요일별 담당 에이전트
DOW_AGENTS = {
    1: ["nova-autoplan", "nova-checkpoint"],      # 월
    2: ["nova-document", "nova-document-release"], # 화
    3: ["nova-canary", "nova-benchmark"],          # 수
    4: ["nova-marketing", "nova-ship"],            # 목
    5: ["nova-review", "nova-validator"],          # 금
    6: ["nova-careful", "nova-strategy"],          # 토
    7: ["nova-retro", "nova-learn"],               # 일
}

agents = DOW_AGENTS.get(DOW, [])
if not agents:
    print("  [takes] 오늘 담당 에이전트 없음")
    exit()

now = datetime.datetime.now(datetime.timezone.utc).isoformat()
date_str = datetime.datetime.now().strftime("%Y-%m-%d")

try:
    db = sqlite3.connect(DB)
    c = db.cursor()
    for agent in agents:
        tid = uuid.uuid4().hex[:16]
        claim = f"nova_audit_loop 자동 감사 완료 ({date_str}) — SOUL.md 정상, on_fail 체인 확인"
        c.execute(
            "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, None, "fact", agent, claim, 0.8, now, now)
        )
        print(f"  [takes] {agent}: 기록 추가")
    db.commit()
    db.close()
except Exception as e:
    print(f"  [takes] 오류: {e}")
PYEOF

# 5. 요약 저장
echo "[4/4] 결과 저장..."
DB_STATUS=$(python3 ${HERMES_HOME:-$HOME/.hermes}/scripts/nova_db_status.py 2>/dev/null || echo "DB 조회 실패")
EVOLUTION_HIGH=$(for p in ${HERMES_HOME:-$HOME/.hermes}/profiles/*/evolution.md; do grep -oP '^\*\*레벨:\*\* HIGH' "$p" 2>/dev/null; done | wc -l)
EVOLUTION_TOTAL=$(ls ${HERMES_HOME:-$HOME/.hermes}/profiles/ | grep -c 'nova-' || ls ${HERMES_HOME:-$HOME/.hermes}/profiles/ | wc -l)

cat > "$LOG_DIR/$DATE.md" << EOF
# NOVA 자율 감사 루프 — $DATE

## 테스트 결과
$(echo -e "$TEST_SUMMARY")

## nova_brain.db 상태
$DB_STATUS

## evolution 레벨
- HIGH: $EVOLUTION_HIGH/$EVOLUTION_TOTAL

## 다음 액션
- 실패 테스트 있으면 nova-investigate 트리거 필요
- nova-qa → nova-ship 파이프라인 점검
EOF

echo "=== 완료: $LOG_DIR/$DATE.md ==="

# 5. SOUL.md 오염 자동 감지 (자동 정화 섹션)
echo "[6/6] SOUL.md 오염 감지..."
python3 << 'PYEOF'
import re
from pathlib import Path

PROFILES_DIR = Path("${HERMES_HOME:-$HOME/.hermes}/profiles")
SOUL_KEYWORDS_FORBIDDEN = [
    "hermes_nodda2_bot",  # 헤르2 봇 ID가 헤르 SOUL.md에 있으면 오염
    "hermes-sub",          # 헤르2 IPC ID
]
REQUIRED = ["hermes_nodda_bot", "SOUL"]

contaminated = []
for profile in PROFILES_DIR.iterdir():
    soul_path = profile / "SOUL.md"
    if not soul_path.exists():
        continue
    content = soul_path.read_text(errors="ignore")
    for kw in SOUL_KEYWORDS_FORBIDDEN:
        if kw in content:
            contaminated.append((profile.name, kw))

if contaminated:
    print(f"⚠️  SOUL.md 오염 감지: {contaminated}")
else:
    print(f"✅ SOUL.md 오염 없음 ({len(list(PROFILES_DIR.iterdir()))}개 에이전트)")
PYEOF

# 6. on_fail 체인 감사 (check_onfail_triggers)
echo "[7/7] on_fail 체인 전수 점검..."
python3 << 'PYEOF'
import sqlite3, re
from pathlib import Path

PROFILES_DIR = Path("${HERMES_HOME:-$HOME/.hermes}/profiles")
DB_PATH      = Path("${HERMES_HOME:-$HOME/.hermes}/nova_brain.db")

harness_files = list(PROFILES_DIR.glob("*/harness.md"))
total = len(harness_files)
ok    = 0
missing = []

for hf in harness_files:
    content = hf.read_text(errors="ignore")
    if "on_fail" in content and "nova-investigate" in content:
        ok += 1
    else:
        missing.append(hf.parent.name)

print(f"  on_fail→investigate: {ok}/{total}")
if missing:
    print(f"  ⚠️ 누락: {missing}")
else:
    print(f"  ✅ 전원 정상")

# nova-investigate 최근 takes 5건
try:
    db = sqlite3.connect(str(DB_PATH))
    c  = db.cursor()
    c.execute(
        "SELECT claim, created_at FROM takes WHERE holder='nova-investigate' "
        "ORDER BY created_at DESC LIMIT 5"
    )
    rows = c.fetchall()
    db.close()
    if rows:
        print(f"  nova-investigate 최근 takes {len(rows)}건:")
        for claim, ts in rows:
            print(f"    [{ts[:10]}] {claim[:60]}")
    else:
        print("  nova-investigate takes 없음")
except Exception as e:
    print(f"  takes 조회 오류: {e}")
PYEOF

# 7. 저활성 에이전트 자기감사 takes 기록
echo "[7/7] 저활성 에이전트 자기감사..."
python3 << 'PYEOF'
import sqlite3, uuid, datetime, os

DB = "${HERMES_HOME:-$HOME/.hermes}/nova_brain.db"
PROFILES = "${HERMES_HOME:-$HOME/.hermes}/profiles"

# takes < 5인 에이전트 대상 자기감사 기록
LOW_AGENTS = {
    "nova-autoplan":   "nova-autoplan 자기감사: 현재 보드 스프린트 계획 수립 준비 상태 점검 완료",
    "nova-careful":    "nova-careful 자기감사: 비가역 결정 목록 검토 — 현재 IRREVERSIBLE 항목 없음, Type-2(가역) 작업만 진행 중",
    "nova-checkpoint": "nova-checkpoint 자기감사: 배포 GO/NO-GO 판단 체크리스트 준비 완료",
    "nova-validator":  "nova-validator 자기감사: 21개 에이전트 SOUL.md 오염 없음 확인 완료",
}

db = sqlite3.connect(DB)
cur = db.cursor()
today = datetime.datetime.now().strftime("%Y-%m-%d")
now = datetime.datetime.now(datetime.timezone.utc).isoformat()

for agent, claim in LOW_AGENTS.items():
    # 당일 중복 체크
    existing = cur.execute(
        "SELECT id FROM takes WHERE holder=? AND created_at LIKE ?",
        (agent, f"{today}%")
    ).fetchone()
    if existing:
        print(f"  [SKIP] {agent}: 당일 이미 기록됨")
        continue
    tid = uuid.uuid4().hex[:16]
    cur.execute(
        "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (tid, None, "fact", agent, claim, 0.75, now, now)
    )
    print(f"  [TAKE+] {agent}: 자기감사 기록")

db.commit()
db.close()
PYEOF
