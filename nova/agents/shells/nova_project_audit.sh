#!/bin/bash
# NOVA 3개 프로젝트 자동 감사 스크립트
# 매 6시간마다 실행 — 결과를 KB에 저장 + 문제 있을 때만 텔레그램 알림
# Usage: nova_project_audit.sh

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DATE=$(date +%Y-%m-%d)
KB_DIR="${HERMES_HOME:-$HOME/.hermes}/kb/projects"
AUDIT_FILE="${KB_DIR}/nova-audit-${TIMESTAMP}.md"
NOVA_BRAIN="${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_cli.py"

mkdir -p "$KB_DIR"

ISSUES=""
SUMMARY=""

audit_python_project() {
    local name="$1"
    local path="$2"
    local findings=""

    echo "## ${name}" >> "$AUDIT_FILE"
    echo "" >> "$AUDIT_FILE"

    # --- 기능성: 테스트 실행 ---
    echo "### A. 테스트 현황" >> "$AUDIT_FILE"
    TEST_OUT=$(cd "$path" && python3 -m pytest tests/ -q --tb=line 2>&1 | tail -15 || true)
    echo '```' >> "$AUDIT_FILE"
    echo "$TEST_OUT" >> "$AUDIT_FILE"
    echo '```' >> "$AUDIT_FILE"
    FAIL_COUNT=$(echo "$TEST_OUT" | grep -c 'FAILED\|ERROR' 2>/dev/null || true)
    FAIL_COUNT=${FAIL_COUNT:-0}
    if [ "${FAIL_COUNT}" -gt 0 ] 2>/dev/null; then
        findings="${findings}\n- ⚠️ 테스트 실패 ${FAIL_COUNT}건"
    fi

    # --- 안정성: bare except, 미처리 예외 ---
    echo "### B. 안정성 이슈" >> "$AUDIT_FILE"
    BARE_EXCEPT=$(grep -rn 'except:$\|except Exception:$' "${path}/src" --include='*.py' 2>/dev/null | head -10 || true)
    if [ -n "$BARE_EXCEPT" ]; then
        findings="${findings}\n- ⚠️ bare except 발견:\n$(echo "$BARE_EXCEPT" | head -5)"
        echo "$BARE_EXCEPT" >> "$AUDIT_FILE"
    else
        echo "bare except 없음 ✅" >> "$AUDIT_FILE"
    fi

    # Race condition 취약 패턴 (전역변수 + async)
    RACE=$(grep -rn 'global \|asyncio.sleep(0)\|time.sleep(' "${path}/src" --include='*.py' 2>/dev/null | grep -v '#' | head -5 || true)
    if [ -n "$RACE" ]; then
        findings="${findings}\n- 🔍 race condition 후보: $(echo "$RACE" | wc -l)건"
        echo "Race condition 후보:" >> "$AUDIT_FILE"
        echo "$RACE" >> "$AUDIT_FILE"
    fi

    # --- 보안: OWASP Top 10 기준 ---
    echo "### C. 보안 이슈" >> "$AUDIT_FILE"

    # 하드코딩 시크릿
    HARDCODED=$(grep -rn 'password\s*=\s*["'"'"'][^"'"'"']\+\|secret\s*=\s*["'"'"'][^"'"'"']\+\|api_key\s*=\s*["'"'"'][^"'"'"']\+' \
        "${path}/src" --include='*.py' 2>/dev/null | grep -v 'os.environ\|getenv\|test\|#' | head -10 || true)
    if [ -n "$HARDCODED" ]; then
        findings="${findings}\n- 🚨 하드코딩 시크릿 의심: $(echo "$HARDCODED" | wc -l)건"
        echo "하드코딩 시크릿 의심:" >> "$AUDIT_FILE"
        echo "$HARDCODED" >> "$AUDIT_FILE"
    else
        echo "하드코딩 시크릿 없음 ✅" >> "$AUDIT_FILE"
    fi

    # SQL Injection 취약 패턴
    SQLI=$(grep -rn 'execute.*%s\|execute.*format\|f"SELECT\|f"INSERT\|f"UPDATE\|f"DELETE' \
        "${path}/src" --include='*.py' 2>/dev/null | head -10 || true)
    if [ -n "$SQLI" ]; then
        findings="${findings}\n- 🚨 SQL Injection 취약 패턴: $(echo "$SQLI" | wc -l)건"
        echo "SQL Injection 패턴:" >> "$AUDIT_FILE"
        echo "$SQLI" >> "$AUDIT_FILE"
    else
        echo "SQL Injection 취약 패턴 없음 ✅" >> "$AUDIT_FILE"
    fi

    # 입력 검증 누락 (Pydantic validator 없는 라우트)
    NO_VALID=$(grep -rn '@app\.\|@router\.' "${path}/src" --include='*.py' -A 3 2>/dev/null | \
        grep -B 2 'request.json\|request.form\|request.args' | grep -v 'Pydantic\|BaseModel\|validator' | head -5 || true)

    # --- 사용성: 에러 응답 일관성 ---
    echo "### D. 사용성 이슈" >> "$AUDIT_FILE"
    ERR_INCONSIST=$(grep -rn 'return {"error"\|return {"message"\|HTTPException\|raise ValueError' \
        "${path}/src" --include='*.py' 2>/dev/null | head -10 || true)
    ERR_COUNT=$(echo "$ERR_INCONSIST" | grep -c '.' || echo 0)
    echo "에러 응답 패턴 ${ERR_COUNT}건 확인" >> "$AUDIT_FILE"

    # 결과 저장
    echo "" >> "$AUDIT_FILE"
    if [ -n "$findings" ]; then
        echo "### 🎯 발견된 이슈 요약" >> "$AUDIT_FILE"
        printf "%b\n" "$findings" >> "$AUDIT_FILE"
        ISSUES="${ISSUES}\n[${name}]${findings}"
    else
        echo "### ✅ 주요 이슈 없음" >> "$AUDIT_FILE"
    fi
    echo "" >> "$AUDIT_FILE"
    echo "---" >> "$AUDIT_FILE"
    echo "" >> "$AUDIT_FILE"
}

audit_node_project() {
    local name="$1"
    local path="$2"
    local findings=""

    echo "## ${name}" >> "$AUDIT_FILE"
    echo "" >> "$AUDIT_FILE"

    # 테스트 실행
    echo "### A. 테스트 현황" >> "$AUDIT_FILE"
    TEST_OUT=$(cd "$path" && npm test 2>&1 | tail -15 || true)
    echo '```' >> "$AUDIT_FILE"
    echo "$TEST_OUT" >> "$AUDIT_FILE"
    echo '```' >> "$AUDIT_FILE"
    FAIL_COUNT=$(echo "$TEST_OUT" | grep -c 'FAIL\|✕\|×' || echo 0)
    if [ "$FAIL_COUNT" -gt 0 ]; then
        findings="${findings}\n- ⚠️ 테스트 실패 ${FAIL_COUNT}건"
    fi

    # 보안: 하드코딩 시크릿
    echo "### C. 보안 이슈" >> "$AUDIT_FILE"
    HARDCODED=$(grep -rn "password\s*=\s*['\"][^'\"]\+\|secret\s*=\s*['\"][^'\"]\+" \
        "${path}/src" --include='*.js' 2>/dev/null | grep -v 'process.env\|test\|#' | head -10 || true)
    if [ -n "$HARDCODED" ]; then
        findings="${findings}\n- 🚨 하드코딩 시크릿 의심: $(echo "$HARDCODED" | wc -l)건"
        echo "$HARDCODED" >> "$AUDIT_FILE"
    else
        echo "하드코딩 시크릿 없음 ✅" >> "$AUDIT_FILE"
    fi

    # unhandled promise rejection
    UNHANDLED=$(grep -rn '\.then(\|async function\|await ' "${path}/src" --include='*.js' 2>/dev/null | \
        grep -v '.catch\|try {' | head -10 || true)
    UNHANDLED_COUNT=$(echo "$UNHANDLED" | grep -c '.' || echo 0)
    if [ "$UNHANDLED_COUNT" -gt 5 ]; then
        findings="${findings}\n- 🔍 .catch 없는 Promise 패턴 ${UNHANDLED_COUNT}건 검토 필요"
        echo ".catch 없는 async 패턴 ${UNHANDLED_COUNT}건" >> "$AUDIT_FILE"
    fi

    echo "" >> "$AUDIT_FILE"
    if [ -n "$findings" ]; then
        echo "### 🎯 발견된 이슈 요약" >> "$AUDIT_FILE"
        printf "%b\n" "$findings" >> "$AUDIT_FILE"
        ISSUES="${ISSUES}\n[${name}]${findings}"
    else
        echo "### ✅ 주요 이슈 없음" >> "$AUDIT_FILE"
    fi
    echo "" >> "$AUDIT_FILE"
    echo "---" >> "$AUDIT_FILE"
    echo "" >> "$AUDIT_FILE"
}

# ===== 감사 시작 =====
cat > "$AUDIT_FILE" << HEADER
# NOVA 3개 프로젝트 자동 감사 — ${DATE} ${TIMESTAMP}
> 감사 기준: 기능성(테스트) / 안정성(예외처리) / 보안(OWASP Top 10) / 사용성(API 일관성)

HEADER

audit_python_project "사주담(saju-wellness)" "${HERMES_HOME:-$HOME/.hermes}/projects/saju-wellness"
audit_python_project "멘탈로드(mental-load)" "${HERMES_HOME:-$HOME/.hermes}/projects/mental-load"
audit_node_project "케어링(senior-care)" "${HERMES_HOME:-$HOME/.hermes}/projects/senior-care"

# nova_brain에 기록
python3 "$NOVA_BRAIN" takes add nova-qa "자동감사 완료 ${TIMESTAMP} - 발견이슈: $(echo -e "$ISSUES" | grep -c '⚠️\|🚨' || echo 0)건" 2>/dev/null || true

SUMMARY="NOVA 자동 감사 완료 (${TIMESTAMP})\n감사 파일: ${AUDIT_FILE}"

# 이슈가 있을 때만 stdout 출력 (no_agent 크론: 출력 있으면 알림)
if [ -n "$ISSUES" ]; then
    echo "🔍 NOVA 3개 프로젝트 감사 결과 — 이슈 발견됨 (${TIMESTAMP})"
    echo ""
    printf "%b\n" "$ISSUES"
    echo ""
    echo "상세: ${AUDIT_FILE}"
fi
# 이슈 없으면 silent
