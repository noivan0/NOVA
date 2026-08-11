#!/usr/bin/env python3
import os
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

import json, re, subprocess, sys, time, os, fcntl, sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ── 단일 실행 보장 (brain_watcher ↔ autonomous_engine 동시 트리거 방지)
CHAIN_LOCK_FILE = "/tmp/nova_chain.lock"

# ============================================================
# 에이전트 → NOVA Harness 매핑
# ready 상태 태스크의 assignee가 이 맵에 있으면 harness 자동 실행
# ============================================================
HARNESS_AGENTS = {
    # 자율 지식 탐구
    "nova-research":    "research",
    "nova-autoplan":    "research",
    # 자율 코드 구현
    "nova-dev":         "code_implement",
    # 자율 코드 리뷰 (security+quality 병렬)
    "nova-review":      "code_review",
    # 자율 QA (pytest 실행 + LLM 분석)
    "nova-qa":          "qa",
    # 자율 GO/NO-GO 판정
    "nova-checkpoint":  "go_nogo",
    # 자율 보안 최종 승인 (OWASP 평가)
    "nova-cso":         "security_sign_off",
    # 자율 KPI 평가 (brain_health 기반)
    "nova-evaluator":   "kpi_evaluate",
    # 자율 배포 (NOVA_DEPLOY_CMD / DEPLOY_HEALTH_URL 기반)
    "nova-ship":        "ship",
    # 자율 카나리 모니터링 (CANARY_METRICS_CMD 기반)
    "nova-canary":      "canary",
    # 자율 헬스 모니터링 (brain.db health + DEPLOY_HEALTH_URL)
    "nova-health":      "health",
    # 자율 회고 (brain.db takes + health 기반)
    "nova-retro":       "retro",
    # 자율 지식 학습 (learn_engine + takes 패턴)
    "nova-learn":       "learn",
    # 자율 문서화 (코드 아티팩트 → API 문서 + 릴리즈 노트)
    "nova-document":    "document_gen",
    # 자율 문서 배포 + 루프 재진입 트리거
    "nova-document-release": "document_release",
    # 시스템 자가 감사 — 매 스프린트 완주 후 NOVA 코드베이스 무결성 점검
    # AUDIT_PASS: 정상 재진입 / AUDIT_ISSUES: 이슈 handoff로 다음 스프린트에 반영
    "nova-sysaudit":         "system_audit",
    # 자율 장애 조사 (5 Whys RCA) — CHAIN_FAIL 폴백 에이전트
    "nova-investigate":  "investigate",
    # ── superpowers 신규 harness ────────────────────────────────────
    # 완료 주장 전 신선한 검증 강제 (superpowers Iron Law)
    "nova-validator":    "verification_gate",
    # ── 사이드체인 에이전트 (5개) ──────────────────────────────
    "nova-marketing":    "go_nogo",           # 시장 가치 검증
    "nova-strategy":     "document_gen",      # 전략 문서
    "nova-careful":      "security_sign_off", # 위험 분석
    "nova-validator":    "qa",                # 통합 검증
    "nova-benchmark":    "kpi_evaluate",      # 성능 실측
}

def _execute_harness_for_agent(agent: str, context: dict = None) -> bool:
    harness_name = HARNESS_AGENTS.get(agent)
    if not harness_name:
        return False
    try:
        import sys as _sys
        from pathlib import Path as _Path
        nova_src   = _Path.home() / "nova"
        hermes_bin = _Path(os.environ.get("HERMES_HOME", str(_Path.home()/".hermes"))) / "bin"
        for p in (str(hermes_bin), str(nova_src)):
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from nova.core.config import load_config
        from nova.core.harness import HarnessLoader
        from nova.core.orchestrator import Orchestrator
        nova_home = _Path(os.environ.get("NOVA_HOME", str(_Path.home()/".nova")))
        cfg = load_config(str(nova_home/"nova.yaml"))
        cfg.harnesses_dir = str(_Path(cfg.harnesses_dir).expanduser())
        cfg.workspace     = str(_Path(cfg.workspace).expanduser())
        loader  = HarnessLoader(cfg.harnesses_dir)
        harness = loader.load(harness_name)
        orch    = Orchestrator(cfg)
        ok      = orch.run(harness, context=context or {}, resume=False)
        log(f"  [HARNESS] {agent} → {harness_name}: {'OK' if ok else 'FAIL'}")
        # BUG-D3 수정: harness 성공 후 kb_sync로 brain.db 인덱싱 (BUG-NEW-2: returncode 체크 추가)
        if ok:
            hermes_home = _Path(os.environ.get("HERMES_HOME", str(_Path.home()/".hermes")))
            hermes_bin  = hermes_home / "bin"
            nova_src    = _Path.home() / "nova"
            # sqlite_vec가 설치된 python3 우선 (Hermes venv에는 sqlite_vec 없음 — agent_worker 동일 방식)
            for _py in ("/usr/bin/python3", "/usr/local/bin/python3", _sys.executable):
                if not _Path(_py).exists():
                    continue
                sync_r = subprocess.run(
                    [_py, str(hermes_bin / "nova_kb_sync.py")],
                    env={**os.environ,
                         "HERMES_HOME": str(hermes_home),
                         "NOVA_HOME":   str(nova_home),
                         "PYTHONPATH":  str(hermes_bin)+":"+str(nova_src)},
                    capture_output=True, text=True, timeout=120,
                )
                if sync_r.returncode == 0:
                    break
                if "sqlite_vec" not in sync_r.stderr:
                    log(f"  [kb_sync] FAIL rc={sync_r.returncode} {(sync_r.stderr or sync_r.stdout)[:80]}")
                    break
        return ok
    except Exception as e:
        log(f"  [HARNESS-ERR] {agent} → {e}")
        return False

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
    "nova-sysaudit",          # SYSTEM-AUDIT — 매 라운드 완주 후 자가 점검
]

# ============================================================
# 정방향 체인 (on_done — DoD 통과 후만)
# ============================================================
CHAIN_DONE = {
    "nova-autoplan":          "nova-dev",
    # nova-dev → CHAIN_FORK로 이관 (review+cso 병렬 분기)
    # nova-review → CHAIN_FORK 이후 JOIN으로 합류 (nova-cso와 병렬)
    "nova-cso":               "nova-qa",   # JOIN fallback 역할 유지
    "nova-qa":                "nova-ship",
    "nova-ship":              "nova-checkpoint",
    "nova-checkpoint":        "nova-canary",   # 기본 체인 (CHAIN_FORK가 우선 적용)
    # canary/health/retro/document → JOIN 처리 (CHAIN_JOIN)
    # CHAIN_DONE에서 제거해 FORK 분기 에이전트가 독자적으로 다음 에이전트를 생성하지 않도록
    "nova-canary":            "",   # JOIN: canary+health 둘 다 완료 → evaluator
    "nova-health":            "",   # JOIN: 위와 동일
    "nova-evaluator":         "nova-retro",   # 기본 체인 (CHAIN_FORK가 우선)
    "nova-retro":             "",   # JOIN: retro+document 둘 다 완료 → document-release
    "nova-document":          "",   # JOIN: 위와 동일
    "nova-learn":             "nova-document",
    "nova-document-release":  "nova-sysaudit",  # ← 라운드 완주 → 시스템 감사 → 새 스프린트

    # 시스템 자가 감사: document-release 완료 후 시스템 상태 점검
    # AUDIT_PASS → nova-autoplan 재진입 (정상 스프린트)
    # AUDIT_ISSUES → nova-dev에 수정 지시 (시스템 버그 수정 스프린트)
    "nova-sysaudit":          "nova-autoplan",

    # 사이드 체인
    "nova-research":          "nova-strategy",
    "nova-marketing":         "nova-strategy",
    "nova-strategy":          "nova-autoplan",
    "nova-investigate":       "nova-dev",
    "nova-careful":           "nova-dev",
    "nova-validator":         "nova-ship",
    "nova-benchmark":         "nova-evaluator",
}

# ============================================================
# 병렬 분기 체인 — 한 에이전트 완료 시 여러 에이전트를 동시 생성
# 이 테이블에 있는 에이전트는 CHAIN_DONE 대신 CHAIN_FORK 사용
# ============================================================
CHAIN_FORK: dict[str, list[str]] = {
    # nova-dev 완료 → review + cso 동시 생성 (병렬 리뷰/보안 검증)
    "nova-dev":        ["nova-review", "nova-cso"],
    # nova-checkpoint 완료 → canary + health 동시 생성 (병렬 모니터링)
    "nova-checkpoint": ["nova-canary", "nova-health"],
    # nova-evaluator 완료 → retro + learn 병렬 생성
    # nova-learn 완료 → CHAIN_DONE("nova-document") → nova-document 생성
    # nova-retro + nova-document 완료 → JOIN → nova-document-release
    "nova-evaluator":  ["nova-retro", "nova-learn"],
}

# ============================================================
# CHAIN_JOIN — FORK 분기 합류 테이블
# {합류 에이전트: [반드시 완료돼야 할 에이전트 목록]}
# 이 목록의 에이전트가 모두 done 상태일 때만 합류 에이전트 생성
# ============================================================
CHAIN_JOIN: dict[str, list[str]] = {
    # review + cso 둘 다 완료 → nova-qa 합류
    "nova-qa":                ["nova-review", "nova-cso"],
    # canary + health 둘 다 완료 → evaluator 합류
    "nova-evaluator":         ["nova-canary", "nova-health"],
    # retro + document 둘 다 완료 → document-release 합류
    "nova-document-release":  ["nova-retro",  "nova-document"],
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
    "nova-evaluator":  ("nova-checkpoint", "KPI 미달 — 체크포인트로 돌아가 기준 재설정 후 전략 수정"),
    "nova-document":   ("nova-dev",      "문서 생성 실패 — 코드/API 수정 후 재시도"),
    "nova-ship":       ("nova-qa",         "배포 실패 — 코드/스크립트 수정 후 QA→SECURITY→CHECKPOINT 재통과 필수"),
    # ── 사이드체인 에이전트 역방향 ────────────────────────────────
    "nova-marketing":  ("nova-autoplan",  "시장 검증 실패 — 전략 수정된 후 재시도"),
    "nova-strategy":   ("nova-autoplan",  "전략 수립 실패 — 재기획 후 재시도"),
    "nova-careful":    ("nova-autoplan",  "위험 감지 — 기획 단계에서 순환 재실행"),
    "nova-validator":  ("nova-dev",       "검증 실패 — 코드 수정 후 재검증"),
    "nova-benchmark":  ("nova-dev",       "KPI 미달 — 코드 개선 후 재비교"),
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
    "nova-checkpoint": [
        r"(?:(?<![-a-z])go(?![-a-z])|lgtm)",  # GO 또는 LGTM 판정 — go_nogo harness 연동
    ],
    # BUG-NEW-3 수정: nova-evaluator DoD 추가 → BACKWARD_JUMP nova-checkpoint 발동 가능
    "nova-evaluator": [
        r"KPI_(?:PASS|FAIL)",   # kpi_evaluate harness 출력 키워드
    ],
    # nova-ship: ship harness write_dod_summary 출력 키워드
    "nova-ship": [
        "HTTP 200",     # 헬스체크 통과
        "deploy",       # 배포 수행
    ],
    # nova-canary: canary harness write_dod_summary 출력 키워드
    "nova-canary": [
        r"CANARY_(?:OK|ANOMALY)",   # canary 판정
    ],
    # nova-health: health harness write_dod_summary 출력 키워드
    "nova-health": [
        r"HEALTH_(?:OK|DEGRADED)",  # health 판정
    ],
    # nova-retro: generate_retro llm 출력 (report.md)
    "nova-retro": [
        r"(?:What Went Well|\ud68c\uace0|\uac1c\uc120)",  # 회고 키워드 (영/한)
    ],
    # nova-learn: write_learn_summary llm 출력
    "nova-learn": [
        r"(?:Knowledge|learn|\ud559\uc2b5)",              # 학습 키워드
    ],
    # nova-document: write_dod_summary 출력 키워드
    "nova-document": [
        "document",     # 문서화 수행
    ],
    # nova-document-release: write_loop_summary 출력 키워드
    "nova-document-release": [
        "release",      # 릴리즈 수행
        "complete",     # 루프 완결
    ],
    # nova-sysaudit: system_audit harness write_audit_summary 출력 키워드
    "nova-sysaudit": [
        "system_audit",   # 감사 수행
        r"AUDIT_(?:PASS|ISSUES)",  # 최종 판정 (PASS or ISSUES 모두 DoD 통과)
    ],
    # ── 사이드체인 에이전트 DoD ────────────────────────────────
    "nova-marketing":  [r"(?:GO|LGTM|PASS|reject|no-go)"],
    "nova-strategy":   ["document", "strategy"],
    "nova-careful":    ["OWASP", r"CRITICAL[=:\s]\s*0"],
    "nova-validator":  ["passed", r"(?:failed:\s*0|0\s+failed)"],
    "nova-benchmark":  [r"KPI_(?:PASS|FAIL)"],
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
    "nova-sysaudit":          "nova-investigate",  # 감사 실패 → 5Whys 조사
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

# PATH에 ~/.local/bin 추가 (hermes 명령이 여기에 있음 — subprocess 환경 보정)
_local_bin = str(Path.home() / ".local" / "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + ":" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

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
        # 특수문자 포함 키워드는 regex, 그 외는 단순 포함 체크
        # 주: Python dict에 저장된 r"..." 리터럴은 prefix 없이 저장되므로
        #     특수문자 체크(elif)로 자동 처리됨 — r"..." 분기는 dead code
        if any(c in kw for c in r"\.^$*+?{}[]|()"):
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
    """무한루프 감지.

    /roop는 max_sprints=0 무제한이 기본 설계다.
    nova-autoplan/nova-dev 같은 메인 체인 에이전트는 스프린트마다 반복되는 것이 정상.
    따라서 단순 완료 횟수로 루프를 판정하면 정상 동작을 차단한다.

    실제 루프 감지 기준:
      - 같은 태스크 계보(lineage) 안에서 동일 에이전트가 LOOP_DEPTH_MAX회 연속 역방향
      - 즉 짧은 시간 안에 같은 에이전트가 fail→retry→fail→retry 를 반복하는 경우만 감지
      - 정방향 체인(스프린트 전진)은 루프가 아님
    """
    if candidate_agent == "nova-investigate":
        return False

    # 역방향(BACKWARD_JUMP) 태스크만 카운팅 — 정방향 스프린트 진행은 제외
    # title에 "[역방향↩]" 또는 "[LOOP]" 패턴이 있는 것만 역방향 태스크
    backward = [t for t in tasks
                if t.get("assignee") == candidate_agent
                and (str(t.get("title", "")).startswith("[역방향↩]")
                     or str(t.get("title", "")).startswith("[LOOP]"))
                and t.get("status") in ("done", "failed", "blocked", "ready")]
    return len(backward) >= LOOP_DEPTH_MAX


ASSIGN_RULES = [
    # ── 기존 규칙 ────────────────────────────────────────────────
    (["배포", "deploy", "docker", "서버"], "nova-ship"),
    (["테스트", "test", "pytest", "coverage", "qa"], "nova-qa"),
    (["문서", "document", "docs", "release note"], "nova-document"),
    (["감사", "review", "코드 리뷰", "PR"], "nova-review"),
    (["구현", "implement", "개발", "기능", "dev"], "nova-dev"),
    (["성능", "benchmark", "metric", "DORA"], "nova-benchmark"),
    (["canary", "SLO", "헬스", "health", "watchdog"], "nova-health"),
    (["마케팅", "marketing", "SEO", "GEO", "블로그"], "nova-marketing"),
    (["계획", "plan", "sprint", "스프린트", "autoplan"], "nova-autoplan"),
    (["학습", "learn", "evolution", "패턴"], "nova-learn"),
    (["회고", "retro", "retrospective", "KPI"], "nova-retro"),

    # ── gstack 트리거 키워드 (강화된 보안/조사) ─────────────────────
    # gstack CSO: OWASP + STRIDE 통합 보안 감사
    (["보안", "security", "OWASP", "취약", "cso", "STRIDE", "위협모델",
      "injection", "xss", "csrf", "취약점", "penetration", "pentest",
      "보안감사", "trust boundary", "LLM trust", "spoofing", "tampering"], "nova-cso"),
    # gstack Iron Law 조사: 근본원인, 3회 실패
    (["조사", "investigate", "RCA", "원인", "root cause", "근본원인",
      "5 whys", "whys", "디버그", "debug", "버그원인", "장애원인",
      "재현", "reproduce", "증상", "symptom", "실패원인"], "nova-investigate"),
    # gstack review: CI 통과 후 프로덕션 버그 탐지
    (["staff engineer", "production bug", "코드감사", "sql injection",
      "completeness gap", "side effect", "conditional bug",
      "TODO 확인", "placeholder 확인"], "nova-review"),

    # ── superpowers 트리거 키워드 (신규 harness 라우팅) ───────────────
    # verification_gate: 완료 주장 전 검증 강제
    (["검증", "verify", "verification", "완료 확인", "done 확인",
      "fresh verification", "완료 전 검증", "테스트 통과 확인",
      "빌드 확인", "완료 주장"], "nova-validator"),
]

def auto_assign_agent(title: str, body: str) -> str | None:
    text = (title + " " + (body or "")).lower()
    for keywords, agent in ASSIGN_RULES:
        if any(kw.lower() in text for kw in keywords):
            return agent
    return None


# ============================================================
# active=1 고착 자동 복구 (BUG-FAIL-3)
# ============================================================
def _recover_stuck_loop(board: str) -> None:
    """active=1이고 running 태스크가 10분 이상 고착 → kanban block 후 역방향 복구 트리거."""
    import time as _time
    try:
        r = subprocess.run(
            ["hermes", "kanban", "--board", board, "list", "--json"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return
        tasks = json.loads(r.stdout)
        active_cnt  = sum(1 for t in tasks if t.get("status") == "running")
        running_tasks = [t for t in tasks if t.get("status") == "running"]
        if active_cnt != 1 or not running_tasks:
            return
        task = running_tasks[0]
        started_raw = task.get("started_at") or task.get("created_at") or 0
        if not started_raw:
            return
        # ISO 문자열 또는 unix timestamp 처리
        if isinstance(started_raw, str):
            try:
                from datetime import datetime as _dt, timezone as _tz
                started_ts = _dt.fromisoformat(started_raw.replace("Z", "+00:00")).timestamp()
            except Exception:
                return
        else:
            started_ts = float(started_raw)
        elapsed = _time.time() - started_ts
        if elapsed >= 600:  # 10분 이상 고착
            agent   = task.get("assignee", "unknown")
            task_id = task.get("id", "")
            log(f"  [STUCK-RECOVER] {agent} {elapsed/60:.1f}분 고착 → kanban block")
            subprocess.run(
                ["hermes", "kanban", "--board", board, "block", task_id,
                 f"auto-recover: {elapsed/60:.0f}분 고착"],
                capture_output=True, timeout=5
            )
            log(f"  [STUCK-RECOVER] block 완료 → run_chain에서 역방향 처리 예정")
    except Exception as _e:
        log(f"  [STUCK-RECOVER-ERR] {board}: {_e}")


# ============================================================
# 메인 체인 로직
# ============================================================
def run_chain(board: str):
    log(f"--- [{board}] 체인 점검 (v3.0) ---")

    # BUG-C5 수정: switch+list 방식 → --board 직접 파라미터로 교체
    # switch는 별도 subprocess라 list subprocess에서 효과 없음
    list_r = subprocess.run(
        ["hermes", "kanban", "--board", board, "list", "--json"],
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
    # cancelled는 수동 중단 의사 표현 — 자동 역방향 점프 대상 제외 (BUG-B1 수정)
    # blocked는 failed와 분리: EXTERNAL_DEPENDENCY/DoD-stall은 역방향 점프 대상 아님
    # blocked → stalled_tasks로 분리 처리 (nova-cso DoD 순환 폭발 방지)
    failed_tasks  = [t for t in tasks if t.get("status") == "failed"]
    stalled_tasks = [t for t in tasks if t.get("status") == "blocked"]
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

    # ── ① done → DoD 게이트 → 정방향 next_agent (CHAIN_FORK 병렬 분기 우선)
    for task in done_tasks:
        agent   = task.get("assignee", "")
        task_id = task.get("id", "")
        title   = task.get("title", "")

        # ── CHAIN_FORK 병렬 분기: 단일 에이전트 완료 → 여러 에이전트 동시 생성
        fork_targets = CHAIN_FORK.get(agent)
        if fork_targets:
            new_forks = [ag for ag in fork_targets if ag not in all_active_assignees
                         and (task_id, ag) not in child_exists_set]
            if new_forks:
                log(f"  [FORK→] {agent} → 병렬 분기: {new_forks}")
                for fork_ag in new_forks:
                    fork_title = f"[Fork→] {title[:45]} / {fork_ag}"
                    fork_body  = (
                        f"NOVA 병렬 분기 체인 (CHAIN_FORK)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"트리거 에이전트: {agent} (완료)\n"
                        f"현재 에이전트: {fork_ag} (병렬 실행)\n"
                        f"파트너 에이전트: {[a for a in fork_targets if a != fork_ag]}\n"
                        f"상위 태스크: {task_id}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"★ DoD 키워드: {DOD_REQUIRED_KEYWORDS.get(fork_ag, ['해당 없음'])}\n"
                        f"생성: {datetime.now().isoformat()}"
                    )
                    cmd = ["hermes", "kanban", "--board", board, "create", fork_title,
                           "--assignee", fork_ag, "--parent", task_id, "--body", fork_body]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode == 0:
                        log(f"  [FORK ✓] {fork_ag} 생성 (병렬)")
                        spawned += 1
                        all_active_assignees.add(fork_ag)
                        child_exists_set.add((task_id, fork_ag))
                        record_chain_step(board, agent, fork_ag, task_id, "fork")
            continue  # FORK 처리했으면 CHAIN_DONE 스킵

        # ── CHAIN_JOIN 합류 처리: 여러 에이전트가 모두 완료됐을 때 합류 에이전트 생성
        # done_assignees: 이 board에서 done 상태인 에이전트 집합
        done_assignees = {t.get("assignee", "") for t in tasks if t.get("status") == "done"}
        for join_ag, prereqs in CHAIN_JOIN.items():
            if join_ag in all_active_assignees:
                continue
            if agent not in prereqs:
                continue
            # 전제 에이전트 모두 done인지 확인
            if all(p in done_assignees for p in prereqs):
                # child_exists_set으로 1차 방어 (BUG-A2 수정: FORK와 동일 방식)
                if (task_id, join_ag) in child_exists_set:
                    continue
                # BUG-JOIN-1 수정 (2026-07-30): "done" 포함 시 이전 라운드 evaluator가
                # 차단 → ACTIVE_STATUSES만 체크. 이미 done된 이전 라운드는 재진입 허용.
                # prereqs 중 가장 최신 created_at 이후에 생성된 join_ag task만 중복으로 간주
                # BUG-JOIN-2 수정 (2026-07-30): prereqs 중 done 없을 때 max() 빈 sequence ValueError
                # default=0 으로 방어
                prereq_max_ts = max(
                    (
                        (t.get("created_at") or 0) for t in tasks
                        if t.get("assignee") in prereqs and t.get("status") == "done"
                    ),
                    default=0
                )
                existing_join = [t for t in tasks if t.get("assignee") == join_ag
                                 and t.get("status") in ACTIVE_STATUSES
                                 and (t.get("created_at") or 0) >= prereq_max_ts]
                if not existing_join:
                    join_title = f"[Join→] {'+'.join(prereqs)} → {join_ag}"
                    join_body  = (
                        f"NOVA 병렬 합류 체인 (CHAIN_JOIN)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"완료된 분기: {prereqs}\n"
                        f"합류 에이전트: {join_ag}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"★ DoD 키워드: {DOD_REQUIRED_KEYWORDS.get(join_ag, ['해당 없음'])}\n"
                        f"생성: {datetime.now().isoformat()}"
                    )
                    cmd = ["hermes", "kanban", "--board", board, "create", join_title,
                           "--assignee", join_ag, "--body", join_body]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode == 0:
                        log(f"  [JOIN ✓] {prereqs} 완료 → {join_ag} 합류 생성")
                        spawned += 1
                        all_active_assignees.add(join_ag)
                        record_chain_step(board, "+".join(prereqs), join_ag, task_id, "join")

        next_ag = CHAIN_DONE.get(agent)
        if not next_ag:
            continue

        # ── nova-sysaudit → nova-autoplan: KPI_PASS/max_sprints 체크 ──
        # BUG-1 수정: 체인이 doc-release→sysaudit→autoplan으로 변경됨
        # KPI_PASS 체크는 반드시 nova-autoplan 재진입 직전(nova-sysaudit→autoplan)에 있어야 함
        if agent == "nova-sysaudit" and next_ag == "nova-autoplan":
            _nova_home = Path(os.environ.get("NOVA_HOME", str(Path.home() / ".nova")))
            # BUG-KPI-STALE 수정 (2026-08-11): kpi_evaluate harness가 panel judge 구조로
            # 개편되며 evaluate_kpi phase의 output_file이 kpi_report.md → kpi_report_claude.md로
            # 변경됨. 이후 kpi_report.md는 더 이상 갱신되지 않는데 이 판정 로직만 옛 파일을
            # 그대로 봐서, 과거(예: 21시간 전)의 stale KPI_PASS가 영구 박제되어 매 체인마다
            # nova-autoplan 재진입을 잘못 차단하는 회귀가 발생함(자율루프 완전 정지).
            # 수정: dod_verify가 실제로 쓰는 report.md(항상 최신 판정 반영)를 최우선으로 보고,
            # 없을 때만 구 kpi_report.md로 폴백.
            _kpi_report_new = _nova_home / "workspace" / "kpi_evaluate" / "report.md"
            _kpi_rpt = _nova_home / "workspace" / "kpi_evaluate" / "kpi_report.md"
            _kpi_text = ""
            if _kpi_report_new.exists():
                _kpi_text = _kpi_report_new.read_text(errors="replace")
            elif _kpi_rpt.exists():
                _kpi_text = _kpi_rpt.read_text(errors="replace")
            # KPI_PASS/KPI_FAIL 둘 다 있으면(구 dod_verify 하드코딩 잔재) 마지막 등장 토큰 기준
            _last_pass_idx = _kpi_text.rfind("KPI_PASS")
            _last_fail_idx = _kpi_text.rfind("KPI_FAIL")
            if _kpi_text and _last_pass_idx > _last_fail_idx:
                log(f"  [ROOP COMPLETE] KPI_PASS 감지 ({_kpi_report_new.name if _kpi_report_new.exists() else _kpi_rpt.name}) — nova-autoplan 재진입 차단, 루프 종료")
                continue
            # max_sprints 체크
            _state_f = _nova_home / "logs" / "roop_state.json"
            if _state_f.exists():
                try:
                    _st = json.loads(_state_f.read_text())
                    _max_s  = _st.get("max_sprints", 0)
                    _sp_n   = _st.get("sprint", 1)
                    if _max_s > 0 and _sp_n >= _max_s:
                        log(f"  [ROOP] max_sprints({_max_s}) 도달 — 루프 종료")
                        continue
                    _st["sprint"] = _sp_n + 1
                    _state_f.write_text(json.dumps(_st, ensure_ascii=False, indent=2))
                except Exception as _e:
                    import sys as _sys
                    print(f"[chain_engine] 스프린트 카운터 증가 실패: {_e}", file=_sys.stderr)

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

    # ── ②-b stalled(blocked) → 모니터링만 (역방향 점프 금지)
    # blocked는 DoD 미달 일시 정지 상태 — 자동 역방향 점프 시 순환 폭발 위험
    # nova-review/nova-cso가 ready로 바뀌면 체인이 자연스럽게 재시도
    stalled_cnt = len(stalled_tasks)
    if stalled_cnt >= 1:
        stalled_agents = [t.get("assignee") for t in stalled_tasks]
        lvl = "WARN" if stalled_cnt > 3 else "INFO"
        log(f"  [STALLED-{lvl}] blocked {stalled_cnt}개: {set(stalled_agents)}" + (" — 수동 확인 권장" if stalled_cnt > 3 else ""))

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

    # ── ⑤ HARNESS_AGENTS: ready 태스크 → nova_orchestrator.py 통해 독립 subprocess 파견
    # ★ BUG-F 수정: chain 실행 도중 생성된 ready 태스크를 포함하기 위해 kanban을 재조회
    # (run_chain 시작 시점과 체인 처리 완료 시점의 태스크 목록이 다름)
    fresh_r = subprocess.run(
        ["hermes", "kanban", "--board", board, "list", "--json"],
        capture_output=True, text=True
    )
    try:
        fresh_tasks = json.loads(fresh_r.stdout) if fresh_r.returncode == 0 else tasks
    except Exception:
        fresh_tasks = tasks

    ready_harness_tasks = [t for t in fresh_tasks
                           if t.get("status") == "ready"
                           and t.get("assignee") in HARNESS_AGENTS]

    if ready_harness_tasks:
        orchestrator_py = Path(os.environ.get("HERMES_HOME",
                               str(Path.home() / ".hermes"))) / "bin" / "nova_orchestrator.py"
        _hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        env_orch = {
            **os.environ,
            "HERMES_HOME": _hermes_home,
            "NOVA_HOME":   os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")),
            # BUG-D3 수정: HERMES_HOME 환경변수 기준으로 PYTHONPATH 설정 (하드코딩 제거)
            "PYTHONPATH":  str(Path(_hermes_home) / "bin") + ":" + str(Path.home() / "nova"),
            # PATH에 ~/.local/bin 추가 (hermes 명령 위치)
            "PATH": str(Path.home()/".local"/"bin") + ":" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }

        agent_names = [t.get("assignee", "") for t in ready_harness_tasks]
        log(f"  [DISPATCH→ORCH] {len(ready_harness_tasks)}개 에이전트 파견: {agent_names}")

        # orchestrator --dispatch 호출: 내부에서 Popen 후 비데몬 스레드 감시
        # --wait 로 모든 에이전트 완료까지 대기 (워커가 kanban 직접 처리)
        r = subprocess.run(
            [sys.executable, str(orchestrator_py),
             "--dispatch", "--board", board, "--wait"],
            capture_output=True, text=True,
            timeout=3600,  # 최대 1시간 (harness 실행 시간 고려)
            env=env_orch,
        )

        if r.returncode == 0:
            try:
                result = json.loads(r.stdout.strip().split("\n")[-1])
                n = result.get("dispatched", 0)
                log(f"  [ORCH ✓] {n}개 에이전트 완료 (kanban은 워커가 직접 처리)")
            except Exception:
                log(f"  [ORCH ✓] 오케스트레이터 완료")

            # ★ BUG-INOTIFY-DEADEND 수정: orchestrator 완료 후 새로 생긴 ready tasks 재파견
            # 체인 처리 도중 nova-sysaudit 등이 nova-autoplan을 ready로 만들면
            # ⑤번 fresh_tasks는 이를 포함하지 않아 영구 미파견 상태가 됨
            remaining_r = subprocess.run(
                ["hermes", "kanban", "--board", board, "list", "--json"],
                capture_output=True, text=True, timeout=10, env=env_orch
            )
            if remaining_r.returncode == 0 and remaining_r.stdout.strip():
                try:
                    remaining = json.loads(remaining_r.stdout)
                    leftover = [t for t in remaining
                                if t.get("status") == "ready"
                                and t.get("assignee") in HARNESS_AGENTS]
                    if leftover:
                        log(f"  [ORCH-2ND] 신규 ready {len(leftover)}개 발견 → 2차 파견")
                        orch2 = subprocess.run(
                            [sys.executable, str(orchestrator_py),
                             "--board", board, "--dispatch", "--wait"],
                            env=env_orch, capture_output=True, text=True,
                            timeout=300  # 2차 파견은 300초 제한 (1차 3600s와 합산 방지)
                        )
                        if orch2.returncode == 0:
                            log(f"  [ORCH-2ND ✓] 2차 파견 완료")
                        else:
                            log(f"  [ORCH-2ND ERR] {orch2.stderr[:80]}")
                except Exception as e2:
                    log(f"  [ORCH-2ND] 재파견 오류: {e2}")
        else:
            log(f"  [ORCH ERR] 오케스트레이터 실패: {r.stderr[:120]}")
            # Fallback: 기존 방식으로 직접 실행
            log("  [FALLBACK] _execute_harness_for_agent 직접 실행")
            for task in ready_harness_tasks:
                ag  = task.get("assignee", "")
                tid = task.get("id", "")
                ttl = task.get("title", "")
                ok_fb = _execute_harness_for_agent(ag, context={"topic": ttl[:60]})
                if ok_fb:
                    subprocess.run(["hermes", "kanban", "--board", board,
                                    "complete", tid], capture_output=True, text=True)
                else:
                    subprocess.run(["hermes", "kanban", "--board", board,
                                    "block", tid, f"{ag} harness 실패"],
                                   capture_output=True, text=True)

    # ── ⑥ BUG-C-1 수정: HARNESS_AGENTS 미등록 ready 태스크 passthrough 처리
    #    nova-ship / nova-canary / nova-health 등 harness 없는 에이전트가
    #    ready 상태일 때 영구 방치되는 문제 수정.
    #    harness가 추가되면 HARNESS_AGENTS에 등록 → 이 블록은 자동으로 건너뜀.
    STAGE_ORDER_SET = set(STAGE_ORDER)
    passthrough_count = 0
    for task in [t for t in fresh_tasks  # WARN-4 수정: tasks → fresh_tasks (최신 상태 반영)
                 if t.get("status") == "ready"
                 and t.get("assignee") not in HARNESS_AGENTS
                 and t.get("assignee") in STAGE_ORDER_SET]:
        agent   = task.get("assignee", "")
        task_id = task.get("id", "")
        title   = task.get("title", "")
        log(f"  [PASSTHROUGH] {agent} harness 미구현 → 자동 complete 처리 (title={title[:40]})")
        subprocess.run(
            ["hermes", "kanban", "--board", board, "complete", task_id],
            capture_output=True, text=True,
        )
        record_chain_step(board, agent, CHAIN_DONE.get(agent, "?"), task_id, "passthrough")
        passthrough_count += 1
    if passthrough_count:
        log(f"  → [{board}] passthrough {passthrough_count}개 완료 (harness 미구현 에이전트)")


BRAIN_DB = str(Path(os.environ.get("NOVA_HOME", str(Path.home()/".nova"))) / "brain.db")

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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # UTC
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
        # done 태스크 과다 시 자동 정리 (Broken pipe 방지)
        for board in boards:
            try:
                _db_p = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes"))) / "kanban/boards" / board / "kanban.db"
                if _db_p.exists():
                    with sqlite3.connect(str(_db_p), timeout=3) as _db:
                        _done_cnt = _db.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
                    if _done_cnt > 60:
                        with sqlite3.connect(str(_db_p), timeout=3) as _conn2:
                            _old = _conn2.execute(
                                "SELECT id FROM tasks WHERE status='done' ORDER BY created_at ASC LIMIT ?",
                                (_done_cnt - 30,)).fetchall()
                        for (_tid,) in _old:
                            subprocess.run(["hermes", "kanban", "--board", board, "archive", _tid],
                                capture_output=True, timeout=5)
                        log(f"  [CLEANUP] {board}: done {_done_cnt}→{_done_cnt-len(_old)}개 정리")
            except Exception as _e:
                log(f"  [CLEANUP-ERR] {board}: {_e}")
        for board in boards:
            try:
                # active=1 고착 감지 및 자동 복구 (BUG-FAIL-3)
                _recover_stuck_loop(board)
                run_chain(board)
            except Exception as e:
                log(f"[FATAL] {board}: {e}")
        log("==== NOVA 체인 엔진 v3.1 완료 ====")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
