#!/usr/bin/env python3
"""nova_phase0.py — NOVA v1.0 Phase 0 판단 루프.

모든 프로젝트 크론 진입 전에 가볍게 호출하여,
- KB 관련 컨텍스트
- 최근 evolution 히스토리
- 반복 실패 패턴 기반 개선 포인트
를 요약한 뒤 기본적으로 should_run=True 를 반환한다.

사용법:
    from nova_phase0 import run_phase0
    result = run_phase0("blog-pipeline")
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# P0-1 fix: 환경변수 기반 경로 (하드코딩 제거)
HERMES_DIR   = Path(os.environ.get("HERMES_DIR", os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))))
KB_DIR       = HERMES_DIR / "kb"
PROJECTS_DIR = Path(os.environ.get("NOVA_PROJECTS_DIR", str(HERMES_DIR / "projects")))
UNIFIED_SEARCH_PATH = HERMES_DIR / "bin" / "kb_unified_search.py"

# Q3 fix (헤르 의견 반영): HERMES_DIR 미설정 시 경고 로그
import logging as _logging
_init_log = _logging.getLogger("nova.phase0")
if "HERMES_DIR" not in os.environ:
    _init_log.warning("HERMES_DIR not set — using default: %s", HERMES_DIR)

# P0-2: cooldown 설정 (환경변수로 재정의 가능)
# BUG-PH0-1 fix: int() 변환 실패 시 ValueError crash 방지 — 기본값으로 fallback
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        _init_log.warning(
            "환경변수 %s=%r 가 정수가 아님 — 기본값 %d 사용", name, raw, default
        )
        return default

PHASE0_FAIL_THRESHOLD  = _env_int("NOVA_PHASE0_FAIL_THRESHOLD", 3)    # 연속 실패 임계값  # [R10-CX-004-FIX] 모듈 import 시 1회 평가 — 런타임 변경 미반영
PHASE0_COOLDOWN_SECS   = _env_int("NOVA_PHASE0_COOLDOWN_SECS", 1800)  # 30분  # [R10-CX-004-FIX] 모듈 import 시 1회 평가 — 런타임 변경 미반영

# P0-3: unified_search timeout 환경변수화
PHASE0_SEARCH_TIMEOUT  = _env_int("NOVA_PHASE0_SEARCH_TIMEOUT", 20)  # [R10-CX-004-FIX] 모듈 import 시 1회 평가 — 런타임 변경 미반영
FAILURE_PATTERNS = [
    (re.compile(r"실패"), "최근 실패 흔적이 있으니 실행 전 실패 원인과 재현 조건을 먼저 검토하세요."),
    (re.compile(r"\bfail(?:ed|ure)?\b", re.IGNORECASE), "Recent fail markers detected; add a preflight check before running."),
    (re.compile(r"\berror\b", re.IGNORECASE), "오류 이력이 있으니 입력값/환경변수/외부 API 응답을 먼저 검증하세요."),
    (re.compile(r"오류"), "오류 이력이 있으니 입력값/환경변수/외부 API 응답을 먼저 검증하세요."),
    (re.compile(r"\btimeout\b", re.IGNORECASE), "타임아웃 패턴이 있으니 timeout 상향 또는 단계 분할을 고려하세요."),
    (re.compile(r"재시도"), "재시도 흔적이 있으니 idempotency와 resume 지점을 강화하세요."),
    (re.compile(r"\brollback\b", re.IGNORECASE), "롤백 흔적이 있으니 변경 전후 상태 백업과 검증 단계를 넣으세요."),
    (re.compile(r"\bcrash\b", re.IGNORECASE), "비정상 종료 흔적이 있으니 예외 처리와 상태 저장을 강화하세요."),
    (re.compile(r"\bbroken\b", re.IGNORECASE), "손상/파손 흔적이 있으니 산출물 검증 단계를 추가하세요."),
    (re.compile(r"\bskip(?:ped)?\b", re.IGNORECASE), "스킵/누락 흔적이 있으니 선행조건 체크를 명시하세요."),
]
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _validate_project_name(project_name: str) -> str:
    normalized = str(project_name).strip()
    if not normalized or not PROJECT_NAME_RE.fullmatch(normalized):
        raise ValueError(f"invalid project_name: {project_name!r}")
    return normalized


def _run_unified_search(project_name: str) -> str:
    if not UNIFIED_SEARCH_PATH.exists():
        return f"KB search skipped: {UNIFIED_SEARCH_PATH} not found"

    # [R10-CX-005-FIX] 동적 import — 모듈 전역 상태 오염 위험. NOVA_PHASE0_USE_IMPORT=0 시 subprocess 우선.
    if not int(os.environ.get("NOVA_PHASE0_USE_IMPORT", "1")):
        # subprocess fallback 경로 (NOVA_PHASE0_USE_IMPORT=0 으로 동적 import 비활성화 시 진입)
        pass
    else:
        try:
            spec = importlib.util.spec_from_file_location("nova_unified_search", UNIFIED_SEARCH_PATH)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for func_name in ("unified_search", "search", "run_search", "main_search"):
                    func = getattr(module, func_name, None)
                    if callable(func):
                        result = func(project_name)
                        if result is None:
                            continue
                        if isinstance(result, str):
                            return result.strip() or "KB search returned empty string"
                        return _safe_json(result)
        except Exception as exc:
            return f"KB search import failed: {exc}"

    try:
        completed = subprocess.run(
            [sys.executable, str(UNIFIED_SEARCH_PATH), project_name],
            capture_output=True,
            text=True,
            timeout=PHASE0_SEARCH_TIMEOUT,  # P0-3 fix
            check=False,
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            return stdout
        if stderr:
            return f"KB search stderr: {stderr[:800]}"
        return f"KB search executed with exit_code={completed.returncode}, no output"
    except Exception as exc:
        return f"KB search subprocess failed: {exc}"


def _parse_recent_evolution_items(project_name: str, limit: int = 5) -> list[str]:
    evolution_path = PROJECTS_DIR / project_name / "evolution.md"
    if not evolution_path.exists():
        return [f"evolution.md not found for project={project_name}"]

    items: list[str] = []
    try:
        lines = evolution_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [f"evolution read failed for project={project_name}: {exc}"]

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("- ") or line.startswith("* "):
            items.append(line)
        elif line.startswith("|") and line.count("|") >= 2:
            items.append(line)
        elif line.startswith("## "):
            items.append(line)

    if not items:
        return [f"No parsable evolution items in {evolution_path}"]
    return items[-limit:]


def _derive_improvements(recent_items: list[str]) -> list[str]:
    improvements: list[str] = []

    for pattern, guidance in FAILURE_PATTERNS:
        if any(pattern.search(item) for item in recent_items):
            improvements.append(guidance)

    if any("초기 생성" in item or "initial" in item.lower() for item in recent_items):
        improvements.append("초기 단계 프로젝트이므로 실행 전 acceptance criteria와 output location을 명확히 하세요.")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in improvements:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _phase0_state_path(project_name: str) -> Path:
    """phase0 연속 실패 카운트 상태 파일 경로 — nova.py failed_phase.json 재사용 방식 채택"""
    return PROJECTS_DIR / project_name / ".nova" / "phase0_state.json"


def _default_state() -> dict:
    return {"consecutive_failures": 0, "last_failure_ts": 0.0, "cooldown_until": 0.0}


def _load_phase0_state(project_name: str) -> dict:
    p = _phase0_state_path(project_name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            # BUG-PH0-4 fix: 손상된 상태 파일 — 무음 swallow 대신 경고 로그
            import logging as _log
            _log.getLogger("nova_phase0").warning(
                "[%s] phase0_state.json 파싱 실패 (%s) — 기본값 사용", project_name, exc
            )
            # [R10-CX-002-FIX] 손상 파일 자동 격리
            try:
                p.rename(p.with_suffix(".corrupt"))
            except OSError:
                pass
            return _default_state()
    return _default_state()


def _save_phase0_state(project_name: str, state: dict) -> bool:
    """phase0 실패 상태 저장 — 원자적 쓰기 + 실패 시 경고만 (fallback: cooldown 미적용)"""
    p = _phase0_state_path(project_name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            return True
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as e:
        # P0-2 fallback: 디렉토리 read-only 등 — 경고만, cooldown 미적용
        import logging as _log
        _log.getLogger("nova_phase0").warning(
            f"[{project_name}] phase0_state.json 쓰기 실패 ({e}) — cooldown 이번 실행만 미적용"
        )
        return False


def record_phase0_result(project_name: str, success: bool) -> None:
    """Phase 0 결과 기록 — nova.py cmd_run에서 호출하여 cooldown 상태 갱신.
    success=True 시 카운터 리셋. success=False 시 카운터 증가 + 임계 도달 시 cooldown 설정.
    """
    project_name = _validate_project_name(project_name)
    # [R10-CX-001-FIX] record_phase0_result 레이스 수정 — fcntl.flock LOCK_EX
    state_path = _phase0_state_path(project_name)
    try:
        import fcntl as _fcntl_p
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _phase0_lock_f = open(state_path.parent / ".phase0.lock", "a")
        _fcntl_p.flock(_phase0_lock_f, _fcntl_p.LOCK_EX)
    except (ImportError, OSError):
        _phase0_lock_f = None
    try:
        state = _load_phase0_state(project_name)
        # [R10-CX-003-FIX] 연속 실패 타임스탬프 — time.time() 단일 호출 후 재사용
        _now = time.time()  # [R10-CX-001-FIX] time.time() 단일 호출 후 재사용
        if success:
            state["consecutive_failures"] = 0
            state["cooldown_until"] = 0.0
        else:
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            state["last_failure_ts"] = _now
            if state["consecutive_failures"] >= PHASE0_FAIL_THRESHOLD:
                state["cooldown_until"] = _now + PHASE0_COOLDOWN_SECS
        _save_phase0_state(project_name, state)
    finally:
        if _phase0_lock_f:
            try:
                import fcntl as _fcntl_p2; _fcntl_p2.flock(_phase0_lock_f, _fcntl_p2.LOCK_UN)
            except Exception: pass
            _phase0_lock_f.close()


def run_phase0(project_name: str) -> dict[str, Any]:
    project_name = _validate_project_name(project_name)
    log = _logging.getLogger("nova.phase0")

    # P0-2 fix: 연속 실패 cooldown 판단
    state = _load_phase0_state(project_name)
    now = time.time()
    cooldown_until = state.get("cooldown_until", 0.0)
    consecutive_failures = state.get("consecutive_failures", 0)
    if cooldown_until > now:
        remaining = int(cooldown_until - now)
        reason = (
            f"[cooldown] {project_name} 연속 {consecutive_failures}회 실패 → "
            f"{remaining}초 대기 중 (NOVA_PHASE0_COOLDOWN_SECS={PHASE0_COOLDOWN_SECS}). "
            f"cooldown_until={int(cooldown_until)}"
        )
        log.warning("[%s] should_run=False: %s", project_name, reason)  # [R17-CC-002-FIX] 로깅 추가
        return {
            "should_run": False,
            "reason": reason,
            "context": "",
            "improvements": [],
            "cooldown_remaining_secs": remaining,
            "consecutive_failures": consecutive_failures,
        }

    # [R17-CC-002-FIX] 다층 조건 1: 연속 실패 수 환경변수 기반 임계 재확인
    _fail_threshold = _env_int("NOVA_PHASE0_FAIL_THRESHOLD", PHASE0_FAIL_THRESHOLD)
    if consecutive_failures >= _fail_threshold and cooldown_until == 0.0:
        # cooldown 미설정 상태에서 임계 초과 — 안전하게 차단
        reason = (
            f"[fail_threshold] {project_name} 연속 실패 {consecutive_failures}회 "
            f">= 임계 {_fail_threshold}회 (NOVA_PHASE0_FAIL_THRESHOLD={_fail_threshold}) "
            f"cooldown_until 미설정 — 안전 차단"
        )
        log.warning("[%s] should_run=False: %s", project_name, reason)  # [R17-CC-002-FIX]
        return {
            "should_run": False,
            "reason": reason,
            "context": "",
            "improvements": [],
            "cooldown_remaining_secs": 0,
            "consecutive_failures": consecutive_failures,
        }

    # [R17-CC-002-FIX] 다층 조건 2: CPU 과부하 체크 (psutil 없으면 skip)
    try:
        import psutil as _psutil
        cpu_pct = _psutil.cpu_percent(interval=0.5)
        _cpu_limit = float(os.environ.get("NOVA_PHASE0_CPU_LIMIT", "90.0"))
        if cpu_pct >= _cpu_limit:
            reason = (
                f"[cpu_overload] CPU {cpu_pct:.1f}% >= 한계 {_cpu_limit}% "
                f"(NOVA_PHASE0_CPU_LIMIT={_cpu_limit}) — 실행 연기"
            )
            log.warning("[%s] should_run=False: %s", project_name, reason)  # [R17-CC-002-FIX]
            return {
                "should_run": False,
                "reason": reason,
                "context": "",
                "improvements": [],
                "cooldown_remaining_secs": 0,
                "consecutive_failures": consecutive_failures,
                "cpu_percent": cpu_pct,
            }
    except ImportError:
        log.debug("[%s] psutil 없음 — CPU 과부하 체크 skip", project_name)  # [R17-CC-002-FIX]

    # [R17-CC-002-FIX] 다층 조건 3: 마지막 실행 최소 간격 10분 (600초)
    _min_interval = float(os.environ.get("NOVA_PHASE0_MIN_INTERVAL_SECS", "600"))
    last_run_ts = state.get("last_run_ts", 0.0)
    if last_run_ts and (now - last_run_ts) < _min_interval:
        elapsed = int(now - last_run_ts)
        wait_more = int(_min_interval - elapsed)
        reason = (
            f"[min_interval] 마지막 실행 {elapsed}초 전 — 최소 간격 {int(_min_interval)}초 미충족 "
            f"({wait_more}초 후 재실행 가능, NOVA_PHASE0_MIN_INTERVAL_SECS={int(_min_interval)})"
        )
        log.warning("[%s] should_run=False: %s", project_name, reason)  # [R17-CC-002-FIX]
        return {
            "should_run": False,
            "reason": reason,
            "context": "",
            "improvements": [],
            "cooldown_remaining_secs": wait_more,
            "consecutive_failures": consecutive_failures,
            "elapsed_since_last_run_secs": elapsed,
        }

    # 모든 조건 통과 — last_run_ts 갱신하여 간격 보장
    state["last_run_ts"] = now
    _save_phase0_state(project_name, state)

    kb_context = _run_unified_search(project_name)
    recent_evolution = _parse_recent_evolution_items(project_name, limit=5)
    improvements = _derive_improvements(recent_evolution)

    reasons = [
        f"기본 정책상 Phase 0는 should_run=True 입니다 (project={project_name}).",
        f"KB: {'available' if UNIFIED_SEARCH_PATH.exists() else 'skipped'}.",
        f"최근 evolution 항목 {len(recent_evolution)}개를 반영했습니다.",
    ]
    if improvements:
        reasons.append(f"개선 포인트 {len(improvements)}개를 선행 체크로 제안합니다.")

    context = (
        f"[KB]\n{kb_context}\n\n"
        f"[Recent Evolution]\n" + "\n".join(recent_evolution)
    )

    return {
        "should_run": True,
        "reason": " ".join(reasons),
        "context": context,
        "improvements": improvements,
        "consecutive_failures": consecutive_failures,
    }


def _default_project() -> str:
    # BUG-PH0-2 fix: PROJECTS_DIR 없을 시 FileNotFoundError crash 방지
    if not PROJECTS_DIR.exists():
        return "blog-pipeline"
    for evolution in sorted(PROJECTS_DIR.glob("*/evolution.md")):
        return evolution.parent.name
    return "blog-pipeline"


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Usage: nova_phase0.py [project_name]")
        # BUG-PH0-2 fix: PROJECTS_DIR 없을 시 iterdir() crash 방지
        if PROJECTS_DIR.exists():
            print("Projects:", ", ".join(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir()))
        else:
            print("Projects dir not found:", PROJECTS_DIR)
        return 0
    project_name = sys.argv[1] if len(sys.argv) > 1 else _default_project()
    result = run_phase0(project_name)
    print(_safe_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
