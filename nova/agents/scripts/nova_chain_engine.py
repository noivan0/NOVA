import os
#!/usr/bin/env python3
"""
NOVA 하네스 체인 엔진 v3.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
노이반 확정 설계 원칙 (2026-05-29):
  1. 정방향은 순서 엄수 — DoD 통과 없이 다음 단계 이동 불가
  2. 역방향은 점프 허용 — 실패 단계에서 원인 단계로 직접 복귀
  3. LEARN 완료 = 한 라운드 완주 → 새 스프린트 자동 킥오프
  4. 보드 동적 등록 — boards.json으로 프로젝트별 관리

변경 이력:
  v2.0: on_done/on_fail 체인, 병목 감지, 자동 배정
  v3.0: DoD 게이트, 역방향 점프 로직, 동적 보드, 정방향 강제 검증
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import json, re, subprocess, time, os, fcntl, sqlite3
from datetime import datetime
from pathlib import Path

# ── 단일 실행 보장 (brain_watcher ↔ autonomous_engine 동시 트리거 방지)
CHAIN_LOCK_FILE = "/tmp/nova_chain.lock"

# ============================================================
# 9단계 정방향 순서 (정방향 건너뛰기 절대 금지)
# ============================================================
STAGE_ORDER = [
    "nova-autoplan",     # 1-THINK/PLAN
    "nova-dev",          # 2-BUILD
    "nova-review",       # 3-REVIEW
    "nova-cso",          # 4-SECURITY
    "nova-qa",           # 5-QA
    "nova-ship",         # 6-SHIP
    "nova-checkpoint",   # 7-CHECKPOINT (GO/NO-GO)
    "nova-canary",       # 8-MONITOR (점진 배포)
    "nova-health",       # 8-MONITOR (헬스)
    "nova-evaluator",    # 8-MONITOR (KPI)
    "nova-retro",        # 9-LEARN
    "nova-learn",        # 9-LEARN
    "nova-document",     # POST-LEARN
    "nova-document-release",  # POST-LEARN
]

# ============================================================
# 정방향 체인 (on_done — DoD 통과 후만)
# ============================================================
CHAIN_DONE = {
    "nova-autoplan":          "nova-dev",
    "nova-dev":               "nova-review",
    "nova-review":            "nova-cso",
    "nova-cso":               "nova-qa",
    "nova-qa":                "nova-ship",
    "nova-ship":              "nova-checkpoint",
    "nova-checkpoint":        "nova-canary",
    "nova-canary":            "nova-health",
    "nova-health":            "nova-evaluator",
    "nova-evaluator":         "nova-retro",
    "nova-retro":             "nova-learn",
    "nova-learn":             "nova-document",
    "nova-document":          "nova-document-release",
    "nova-document-release":  "nova-autoplan",   # ← 라운드 완주 → 새 스프린트

    # 사이드 체인
    "nova-research":          "nova-strategy",
    "nova-marketing":         "nova-strategy",
    "nova-strategy":          "nova-autoplan",
    "nova-investigate":       "nova-dev",         # 조사 완료 → 재구현 (역방향)
    "nova-careful":           "nova-dev",
    "nova-validator":         "nova-ship",
    "nova-benchmark":         "nova-evaluator",
}

# ============================================================
# 역방향 점프 테이블
# 실패/DoD 미달 시 어느 단계로 돌아갈지 명시
# 규칙: 역방향만 허용. 정방향 건너뛰기 불가.
#       SECURITY/QA는 역방향 건너뛰기도 금지 (항상 통과 필수)
# ============================================================
BACKWARD_JUMP = {
    # (실패 에이전트): (점프 대상, 입구 조건)
    "nova-review":     ("nova-dev",      "REVIEW DoD 미달 — 코드 수정 후 REVIEW부터 재시작"),
    "nova-cso":        ("nova-dev",      "SECURITY CRITICAL 발견 — 패치 후 SECURITY부터 재시작"),
    "nova-qa":         ("nova-dev",      "QA 실패 — 버그 수정 후 SECURITY→QA 재통과 필수"),
    "nova-checkpoint": ("nova-cso",      "GO-NOGO 실패 — 보안/QA 재확인 후 CHECKPOINT 재시도"),
    "nova-canary":     ("nova-dev",      "Canary 이상 — 코드 롤백 후 전 단계 재순환"),
    "nova-health":     ("nova-dev",      "헬스 이상 — 긴급 패치 후 REVIEW부터 재시작"),
    "nova-evaluator":  ("nova-retro",    "KPI 미달 — 회고에서 원인 분석 후 전략 수정"),
    "nova-document":   ("nova-dev",      "문서 생성 실패 — 코드/API 수정 후 재시도"),
    "nova-ship":       ("nova-qa",         "배포 실패 — 코드/스크립트 수정 후 QA→SECURITY→CHECKPOINT 재통과 필수"),
}

# ============================================================
# DoD (Definition of Done) 게이트
# 각 에이전트가 done 선언 시 task body에서 검증
# ============================================================
DOD_REQUIRED_KEYWORDS = {
    "nova-dev": [
        "py_compile",      # 문법 오류 없음
        "IMPORT OK",       # 전체 import 성공
    ],
    "nova-review": [
        r"CRITICAL[=:\s]\s*0",  # 치명 이슈 없음 (=0 / : 0 / 공백 모두 허용)
        r"HIGH[=:\s]\s*0",      # 높음 이슈 없음
    ],
    "nova-cso": [
        "OWASP",                # OWASP 체크 수행
        r"CRITICAL[=:\s]\s*0",  # 보안 치명 이슈 없음
    ],
    "nova-qa": [
        "passed",               # 테스트 통과
        r"(?:failed:\s*0|0\s+failed)",  # pytest: '0 failed' / Jest: 'failed: 0' 모두 허용
    ],
    "nova-ship": [
        "HTTP 200",        # 헬스체크 통과
        "deploy",          # 배포 수행
    ],
    "nova-checkpoint": [
        r"(?<![-a-z])go(?![-a-z])",  # GO 판정 — NO-GO 오매칭 방지 (앞뒤 대시/알파벳 없을 때만)
    ],
}

# ============================================================
# on_fail 체인 (실패 시 → 역방향 점프 or nova-investigate)
# ============================================================
CHAIN_FAIL = {
    "nova-dev":               "nova-investigate",
    "nova-review":            "nova-dev",          # 역방향
    "nova-cso":               "nova-dev",          # 역방향
    "nova-qa":                "nova-dev",          # 역방향
    "nova-ship":              "nova-investigate",
    "nova-checkpoint":        "nova-cso",          # 역방향
    "nova-canary":            "nova-investigate",
    "nova-health":            "nova-investigate",
    "nova-evaluator":         "nova-investigate",
    "nova-retro":             "nova-investigate",
    "nova-learn":             "nova-investigate",
    "nova-document":          "nova-investigate",
    "nova-document-release":  "nova-investigate",
    "nova-autoplan":          "nova-investigate",
    "nova-research":          "nova-investigate",
    "nova-marketing":         "nova-investigate",
    "nova-strategy":          "nova-investigate",
    "nova-benchmark":         "nova-investigate",
    "nova-validator":         "nova-investigate",
    "nova-careful":           "nova-investigate",
}

BOTTLENECK_SEC = 60 * 60  # 60분
LOOP_DEPTH_MAX = 10

# ============================================================
# 동적 보드 관리
# ~/.hermes/kanban/nova_boards.json 에서 보드 목록 로드
# 없으면 기존 3개 앱 기본값 사용
# ============================================================
BOARDS_CONFIG = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kanban/nova_boards.json"

def load_boards() -> list[str]:
    if BOARDS_CONFIG.exists():
        data = json.loads(BOARDS_CONFIG.read_text())
        return data.get("boards", [])
    # 기본값 (하위 호환)
    return ["saju-wellness", "mental-load", "senior-care"]


def register_board(board: str) -> bool:
    """새 보드를 nova_boards.json에 등록.
    A01 방어: board 이름에 경로 분리 문자 포함 시 거부.
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", board):
        log(f"  [SECURITY] register_board 거부: 유효하지 않은 보드 이름 '{board}'")
        return False
    data = {"boards": load_boards()}
    if board not in data["boards"]:
        data["boards"].append(board)
        BOARDS_CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return True
    return False


def load_child_exists_set(board: str) -> set:
    """Return set of (parent_id, child_assignee) for non-cancelled/archived children.
    Used for lineage-aware dedup: prevents duplicate forward/backward tasks for the
    same parent + target assignee pair regardless of child status.
    """
    db_path = Path(f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db")
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("""
                SELECT tl.parent_id, t.assignee
                FROM task_links tl
                JOIN tasks t ON t.id = tl.child_id
                WHERE t.status NOT IN ('cancelled', 'archived')
                  AND t.assignee IS NOT NULL
            """).fetchall()
        return {(r[0], r[1]) for r in rows}
    except Exception:
        return set()


# ============================================================
# 유틸
# ============================================================
LOG_FILE = f"/tmp/nova_chain_{datetime.now().strftime('%Y%m%d')}.log"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_restored_history_stub(task: dict, board: str | None = None) -> bool:
    """실제 이력 복원 stub만 판별.

    과거 구현은 title/body 어디에든 `restored history` 문구가 들어가면 복원 stub로 간주해,
    복원 버그를 설명하는 일반 작업(`Patch false DoD reverse-jump on restored history tasks`)까지
    오탐했다. 복원 stub는 매우 좁게 판별해야 한다.
    """
    if (task.get("created_by") or "").strip().lower() == "nova_restore":
        return True

    result_text = (task.get("result") or "").strip()
    if result_text:
        return False

    # 실제 실행/완료 이력이 있으면 일반 작업으로 본다.
    if latest_completed_run_summary(board, task.get("id")):
        return False

    if (task.get("status") or "").strip().lower() != "done":
        return False

    title = str(task.get("title") or "").strip()
    body = str(task.get("body") or "").strip()
    meta_text = " ".join([title, body]).lower()
    restore_markers = [
        "이력 복원",
        "history restore",
        "restored history",
    ]
    if not any(marker in meta_text for marker in restore_markers):
        return False

    # 복원 stub는 짧은 메타데이터 성격의 카드여야 한다.
    # 긴 조사/패치 본문에서 restore marker를 언급하는 경우는 일반 작업이다.
    return len(title) <= 120 and len(body) <= 160



def latest_completed_run_summary(board: str | None, task_id: str | None) -> str:
    """task.result가 비어 있을 때 board DB의 최신 completed run summary를 가져온다."""
    if not board or not task_id:
        return ""

    db_path = Path(f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db")
    if not db_path.exists():
        return ""

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT summary
                FROM task_runs
                WHERE task_id = ?
                  AND status = 'done'
                  AND summary IS NOT NULL
                  AND TRIM(summary) != ''
                ORDER BY COALESCE(ended_at, 0) DESC, id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""



def resolve_dod_evidence(task: dict, board: str | None = None) -> tuple[str, str]:
    """DoD 검증 텍스트와 출처(task.result 또는 task_runs.summary)를 반환."""
    result_text = (task.get("result") or "").strip()
    if result_text:
        return result_text, "task.result"

    run_summary = latest_completed_run_summary(board, task.get("id"))
    if run_summary:
        return run_summary, "task_runs.summary"

    return "", ""



def check_dod(task: dict, board: str | None = None) -> tuple[bool, list[str]]:
    """
    DoD 게이트: kanban_complete(summary=...)가 저장되는 result 필드 또는
    비어 있을 때 최신 completed run summary에 필수 키워드가 있는지 확인.
    Returns (passed: bool, missing: list[str])

    body/title은 태스크 지시사항 또는 이력 복원 메타데이터일 뿐 완료 증거가 아니며,
    body/title까지 스캔하면 restored-history stub에서 거짓 역방향 점프가 발생한다.
    따라서 이력 복원 stub만 예외적으로 통과시키고, 실제 태스크는 run summary fallback까지 확인한다.
    """
    agent = task.get("assignee", "")
    required = DOD_REQUIRED_KEYWORDS.get(agent)
    if not required:
        return True, []  # DoD 정의 없으면 통과

    evidence_text, _evidence_source = resolve_dod_evidence(task, board)
    if not evidence_text and is_restored_history_stub(task, board):
        return True, []
    search_text = evidence_text.lower()

    def _match(kw: str, text: str) -> bool:
        # r"..." 형태(regex 패턴)는 re.search, 일반 문자열은 단순 포함 체크
        if kw.startswith("r\"") or kw.startswith("r'"):
            pattern = kw[2:-1]
            return bool(re.search(pattern, text, re.IGNORECASE))
        elif any(c in kw for c in r"\.^$*+?{}[]|()"):
            # regex 특수문자 포함 시 regex로 처리
            try:
                return bool(re.search(kw, text, re.IGNORECASE))
            except re.error:
                return kw.lower() in text
        return kw.lower() in text

    missing = [kw for kw in required if not _match(kw, search_text)]
    return len(missing) == 0, missing


def is_backward_allowed(from_agent: str, to_agent: str) -> bool:
    """
    정방향 건너뛰기 차단 검증.
    to_agent가 from_agent보다 뒤 단계이면 False(금지).
    역방향이면 True(허용).
    """
    if from_agent not in STAGE_ORDER or to_agent not in STAGE_ORDER:
        return True  # 사이드 체인은 제한 없음
    from_idx = STAGE_ORDER.index(from_agent)
    to_idx = STAGE_ORDER.index(to_agent)
    return to_idx <= from_idx  # 역방향(같거나 앞)만 허용


def detect_loop(tasks: list, candidate_agent: str) -> bool:
    if candidate_agent == "nova-investigate":
        return False
    recent = [t.get("assignee") for t in tasks[-LOOP_DEPTH_MAX:]]
    return sum(1 for a in recent if a == candidate_agent) >= LOOP_DEPTH_MAX


ASSIGN_RULES = [
    (["배포", "deploy", "docker", "서버"], "nova-ship"),
    (["테스트", "test", "pytest", "coverage", "qa"], "nova-qa"),
    (["보안", "security", "OWASP", "취약", "cso"], "nova-cso"),
    (["문서", "document", "docs", "release note"], "nova-document"),
    (["감사", "review", "코드 리뷰", "PR"], "nova-review"),
    (["구현", "implement", "개발", "기능", "dev"], "nova-dev"),
    (["성능", "benchmark", "metric", "DORA"], "nova-benchmark"),
    (["canary", "SLO", "헬스", "health", "watchdog"], "nova-health"),
    (["마케팅", "marketing", "SEO", "GEO", "블로그"], "nova-marketing"),
    (["조사", "investigate", "RCA", "원인"], "nova-investigate"),
    (["계획", "plan", "sprint", "스프린트", "autoplan"], "nova-autoplan"),
    (["학습", "learn", "evolution", "패턴"], "nova-learn"),
    (["회고", "retro", "retrospective", "KPI"], "nova-retro"),
]

def auto_assign_agent(title: str, body: str) -> str | None:
    text = (title + " " + (body or "")).lower()
    for keywords, agent in ASSIGN_RULES:
        if any(kw.lower() in text for kw in keywords):
            return agent
    return None


# ============================================================
# 메인 체인 로직
# ============================================================
def run_chain(board: str):
    log(f"--- [{board}] 체인 점검 (v3.0) ---")

    switch_r = subprocess.run(
        ["hermes", "kanban", "boards", "switch", board],
        capture_output=True, text=True
    )
    if switch_r.returncode != 0:
        log(f"  [ERROR] 보드 전환 실패: {board}")
        return

    list_r = subprocess.run(
        ["hermes", "kanban", "list", "--json"],
        capture_output=True, text=True
    )
    if list_r.returncode != 0 or not list_r.stdout.strip():
        log(f"  [WARN] {board}: 태스크 목록 빈 값")
        return

    try:
        tasks = json.loads(list_r.stdout)
    except json.JSONDecodeError as e:
        log(f"  [ERROR] JSON 파싱 실패: {e}")
        return

    now = time.time()
    done_tasks    = [t for t in tasks if t.get("status") == "done"]
    failed_tasks  = [t for t in tasks if t.get("status") in ("failed", "blocked", "cancelled")]
    running_tasks = [t for t in tasks if t.get("status") == "running"]

    # "현재 처리 중"인 에이전트만 중복 방지 대상
    # done/blocked/failed/cancelled/archived = 완료 또는 막힘 → 새 태스크 허용
    # running/todo/ready/pending = 진행 중 → 중복 생성 방지
    ACTIVE_STATUSES = {"running", "todo", "ready", "pending", "created"}
    all_active_assignees = {
        t.get("assignee") for t in tasks
        if t.get("status") in ACTIVE_STATUSES
    }

    child_exists_set = load_child_exists_set(board)

    spawned = 0

    # ── ① done → DoD 게이트 → 정방향 next_agent
    for task in done_tasks:
        agent   = task.get("assignee", "")
        task_id = task.get("id", "")
        title   = task.get("title", "")

        next_ag = CHAIN_DONE.get(agent)
        if not next_ag:
            continue

        # 중복 방지: 현재 진행 중인 에이전트
        if next_ag in all_active_assignees:
            log(f"  [SKIP] {agent}→{next_ag}: 이미 존재")
            continue

        # 이미 done된 next_ag 태스크가 있으면 SKIP (반복 체인 방지) — 단, 이 task_id의 자녀인 경우만
        done_next = [t for t in tasks
                     if t.get("assignee") == next_ag
                     and t.get("status") == "done"
                     and (task_id, next_ag) in child_exists_set]
        if done_next:
            log(f"  [SKIP] {agent}→{next_ag}: 이미 done 완료 ({len(done_next)}건)")
            continue

        # ★ DoD 게이트 — 키워드 미달 시 역방향 점프
        dod_passed, missing_kw = check_dod(task, board)
        if not dod_passed:
            jump_target, jump_reason = BACKWARD_JUMP.get(agent, ("nova-investigate", "DoD 미달"))
            log(f"  [DoD FAIL] {agent}: 누락 키워드={missing_kw} → 역방향 점프 → {jump_target}")
            log(f"    입구 조건: {jump_reason}")

            # 역방향 허용 확인
            if not is_backward_allowed(agent, jump_target):
                log(f"  [FORWARD BLOCKED] {agent}→{jump_target}: 정방향 건너뛰기 금지!")
                continue

            # 역방향 태스크 생성
            if (jump_target not in all_active_assignees
                    and (task_id, jump_target) not in child_exists_set):
                back_title = f"[역방향↩] DoD 미달: {agent} → {jump_target}"
                back_body = (
                    f"NOVA 역방향 점프 (v3.0 DoD 게이트 트리거)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"실패 에이전트: {agent}\n"
                    f"실패 태스크: {task_id} ({title[:60]})\n"
                    f"DoD 미달 키워드: {missing_kw}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"입구 조건: {jump_reason}\n\n"
                    f"SECURITY/QA는 역방향 건너뛰기 불가 — 반드시 재통과 필수.\n"
                    f"완료 후 done body에 DoD 키워드 필수 기록.\n"
                    f"생성: {datetime.now().isoformat()}"
                )
                cmd = ["hermes", "kanban", "--board", board, "create", back_title,
                       "--assignee", jump_target, "--parent", task_id, "--body", back_body]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    log(f"  [BACKWARD ✓] {agent} → {jump_target} 역방향 점프 생성")
                    spawned += 1
                    all_active_assignees.add(jump_target)
                    child_exists_set.add((task_id, jump_target))
                    record_chain_step(board, agent, jump_target, task_id, "backward")
            else:
                log(f"  [SKIP-BACKWARD] {agent}→{jump_target}: 이미 lineage 자녀 존재 또는 활성 중")
            continue

        # ★ 정방향 강제 검증 — SECURITY(nova-cso), QA(nova-qa) 건너뛰기 불가
        MANDATORY_STAGES = {"nova-cso", "nova-qa"}  # 이전 단계로 건너뛰기 불가
        skipped_mandatory = False
        if next_ag in CHAIN_DONE:
            from_idx = STAGE_ORDER.index(agent) if agent in STAGE_ORDER else -1
            to_idx = STAGE_ORDER.index(next_ag) if next_ag in STAGE_ORDER else -1
            if from_idx >= 0 and to_idx >= 0:
                skipped = MANDATORY_STAGES.intersection(
                    STAGE_ORDER[from_idx + 1:to_idx]
                )
                if skipped:
                    log(f"  [MANDATORY SKIP BLOCKED] {agent}→{next_ag}: {skipped} 건너뛰기 금지")
                    skipped_mandatory = True

        if skipped_mandatory:
            continue

        # ★ 무한루프 감지
        if detect_loop(tasks, next_ag):
            log(f"  [LOOP DETECTED] {next_ag} → nova-investigate 강제 트리거")
            if "nova-investigate" not in all_active_assignees:
                cmd = ["hermes", "kanban", "--board", board, "create",
                       f"[LOOP] {next_ag} 무한루프 감지", "--assignee", "nova-investigate",
                       "--body", f"R13: {next_ag} 연속 {LOOP_DEPTH_MAX}회 감지. 5Whys RCA 필요."]
                subprocess.run(cmd, capture_output=True, text=True)
                all_active_assignees.add("nova-investigate")
            continue

        # ★ 정상 정방향 체인 생성
        next_title = f"[Chain→] {title[:50]} / {next_ag}"
        next_body = (
            f"NOVA 정방향 체인 (v3.0 — DoD 통과 확인)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"이전 단계: {agent} (DoD 통과 ✅)\n"
            f"현재 단계: {next_ag}\n"
            f"상위 태스크: {task_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"★ 완료 시 task body에 DoD 키워드 반드시 기록:\n"
            f"  {DOD_REQUIRED_KEYWORDS.get(next_ag, ['해당 없음'])}\n\n"
            f"★ 역방향 점프 조건 ({next_ag}):\n"
            f"  {BACKWARD_JUMP.get(next_ag, ('없음', '직접 done 처리'))[1]}\n\n"
            f"생성: {datetime.now().isoformat()}"
        )
        cmd = ["hermes", "kanban", "--board", board, "create", next_title,
               "--assignee", next_ag, "--parent", task_id, "--body", next_body]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"  [CHAIN→ ✓] {agent} → {next_ag} (DoD 통과)")
            spawned += 1
            all_active_assignees.add(next_ag)
            child_exists_set.add((task_id, next_ag))
            record_chain_step(board, agent, next_ag, task_id, "forward")
        else:
            log(f"  [ERROR] 체인 생성 실패({next_ag}): {r.stderr.strip()[:120]}")

    # ── ② failed → 역방향 점프 or nova-investigate
    for task in failed_tasks:
        agent   = task.get("assignee", "")
        task_id = task.get("id", "")
        title   = task.get("title", "")

        # EXTERNAL_DEPENDENCY 태그가 있는 blocked 태스크는 자율 처리 불가 — 무시
        body_str = str(task.get("body", "") or "")
        if "EXTERNAL_DEPENDENCY" in body_str or "TWILIO" in title.upper() or "서버 배포" in title:
            log(f"  [SKIP-EXTERNAL] {agent}: 외부 의존성 blocked — 인간 입력 대기 중")
            continue

        fail_ag = CHAIN_FAIL.get(agent, "nova-investigate")

        # 자기 역방향 순환 방지: nova-investigate → nova-investigate 불가
        if fail_ag == agent:
            log(f"  [SKIP-SELF-LOOP] {agent}: 자기 역방향 순환 방지")
            continue

        if fail_ag in all_active_assignees:
            log(f"  [SKIP-FAIL] {agent}→{fail_ag}: 이미 존재")
            continue

        # 역방향 허용 확인
        if not is_backward_allowed(agent, fail_ag):
            log(f"  [FORWARD BLOCKED] on_fail {agent}→{fail_ag}: 정방향 금지. nova-investigate로 전환")
            fail_ag = "nova-investigate"
            if fail_ag in all_active_assignees:
                continue

        # BACKWARD_JUMP 테이블에서 입구 조건 가져오기
        jump_reason = BACKWARD_JUMP.get(agent, (fail_ag, "실패 — 재구현 필요"))[1]

        fail_title = f"[역방향↩] {agent} 실패 → {fail_ag}"
        fail_body = (
            f"NOVA 역방향 점프 (v3.0 on_fail)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"실패 에이전트: {agent} (상태: {task.get('status')})\n"
            f"실패 태스크: {task_id} ({title[:60]})\n"
            f"점프 대상: {fail_ag}\n"
            f"입구 조건: {jump_reason}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"5 Whys RCA 실행 후:\n"
            f"  - 근본 원인 → 수정 → DoD 키워드 포함하여 done 처리\n"
            f"  - Iron Law: 동일 실패 3회 → 설계 재검토\n"
            f"  - SECURITY/QA는 역방향 건너뛰기 금지 — 항상 재통과 필수\n"
            f"생성: {datetime.now().isoformat()}"
        )
        cmd = ["hermes", "kanban", "--board", board, "create", fail_title,
               "--assignee", fail_ag, "--parent", task_id, "--body", fail_body]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"  [BACKWARD↩ ✓] {agent} 실패 → {fail_ag} 역방향 점프")
            spawned += 1
            all_active_assignees.add(fail_ag)
            record_chain_step(board, agent, fail_ag, task_id, "backward_fail")

    # ── ③ 병목 감지 (60분+ running → nova-investigate)
    for task in running_tasks:
        started_at = task.get("started_at", 0)
        if not started_at or (now - started_at) < BOTTLENECK_SEC:
            continue
        agent = task.get("assignee", "")
        task_id = task.get("id", "")
        elapsed_min = int((now - started_at) / 60)
        if "nova-investigate" in all_active_assignees:
            continue
        cmd = ["hermes", "kanban", "--board", board, "create",
               f"[병목] {agent} {elapsed_min}분 체류 → nova-investigate",
               "--assignee", "nova-investigate",
               "--body", f"병목 감지: {agent} {elapsed_min}분. 태스크: {task_id}. 5Whys RCA."]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"  [BOTTLENECK] {agent} {elapsed_min}분 → nova-investigate")
            all_active_assignees.add("nova-investigate")
            spawned += 1

    # ── ④ unassigned ready 태스크 자동 배정
    for task in [t for t in tasks if t.get("status") == "ready" and not t.get("assignee")]:
        task_id = task.get("id", "")
        title = task.get("title", "")
        agent = auto_assign_agent(title, task.get("body", ""))
        if not agent:
            log(f"  [UNASSIGNED] {task_id}: 자동 배정 불가 ({title[:40]})")
            continue
        cmd = ["hermes", "kanban", "--board", board, "assign", task_id, agent]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"  [AUTO-ASSIGN ✓] {task_id} → {agent}")
            all_active_assignees.add(agent)
            spawned += 1

    log(f"  → [{board}] {spawned}개 체인/점프 처리 완료" if spawned else f"  → [{board}] 신규 체인 없음")


BRAIN_DB = f"{_HERMES_HOME}/nova_brain.db"

def record_chain_step(board: str, from_agent: str, to_agent: str, task_id: str, direction: str = "forward"):
    """체인 스텝 단위 nova_brain.db takes 기록 — DreamCycle 학습 소재"""
    db = None
    try:
        import uuid
        db = sqlite3.connect(BRAIN_DB, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")  # WAL 모드 (Codex: database locked 방지)
        db.execute("PRAGMA busy_timeout=5000")
        c = db.cursor()
        now = datetime.now().astimezone().isoformat()
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")  # UTC
        claim = f"[chain] {board}: {from_agent}→{to_agent} ({direction}) task={task_id[:8]}"
        # 오늘 동일 claim 중복 방지
        existing = c.execute(
            "SELECT id FROM takes WHERE claim=? AND created_at LIKE ?",
            (claim, f"{today}%")
        ).fetchone()
        if not existing:
            tid = uuid.uuid4().hex[:16]
            # weight 동적화 (Codex: 고정 0.8 → evolution 미반영 문제 해소)
            # forward = 정상 체인 진행 (0.85), backward = DoD 역방향 (0.72), backward_fail = on_fail 강제 역방향 (0.65)
            weight = 0.85 if direction == "forward" else (0.65 if direction == "backward_fail" else 0.72)
            c.execute(
                "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (tid, None, "fact", "nova-chain", claim, weight, now, now)
            )
            db.commit()
    except Exception as e:
        log(f"  [CHAIN-TAKE-ERR] {e}")
    finally:
        if db:
            db.close()


def handle_memory_events():
    """hermes_events에서 MEMORY_SLIM 이벤트 확인 (chain 실행 전 처리)"""
    try:
        db = sqlite3.connect(BRAIN_DB, timeout=3)
        c = db.cursor()
        events = c.execute(
            "SELECT id, event_type, title FROM hermes_events WHERE event_type IN ('MEMORY_SLIM','MEMORY_SLIM_FAIL') AND is_read=0"
        ).fetchall()
        for eid, etype, title in events:
            log(f"  [MEMORY-EVENT] {etype}: {title}")
            c.execute("UPDATE hermes_events SET is_read=1 WHERE id=?", (eid,))
        db.commit()
        db.close()
        return len(events)
    except Exception:
        return 0


def main():
    # ── flock: watcher ↔ autonomous_engine 동시 실행 방지 ─────────
    lock_fd = open(CHAIN_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("[chain-engine] flock 획득 실패 — 다른 인스턴스 실행 중. SKIP.")
        lock_fd.close()
        return

    try:
        boards = load_boards()
        log("==== NOVA 체인 엔진 v3.1 시작 ====")
        log(f"  보드: {boards}")
        log(f"  정방향 체인: {len(CHAIN_DONE)}개 | 역방향 점프 테이블: {len(BACKWARD_JUMP)}개")
        log(f"  DoD 게이트 적용 에이전트: {list(DOD_REQUIRED_KEYWORDS.keys())}")
        # MEMORY 이벤트 확인
        mem_events = handle_memory_events()
        if mem_events > 0:
            log(f"  [MEMORY] {mem_events}개 이벤트 처리 완료")
        for board in boards:
            try:
                run_chain(board)
            except Exception as e:
                log(f"[FATAL] {board}: {e}")
        log("==== NOVA 체인 엔진 v3.1 완료 ====")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
