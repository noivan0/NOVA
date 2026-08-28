"""
nova.kernel.scope_drift — Scope drift detection (gstack parity)
==================================================================

gstack의 "Scope Drift Detection: 의도한 것만 변경했나?" 원칙을 결정론적
코드로 재현한다. 지금까지 NOVA의 code_implement/code_review harness는
이 문구를 docstring에만 적어두고 실제 검사 로직이 없었다.

접근 방식:
  - task 설명(요구사항 텍스트, 예: analysis.md)에서 언급된 파일 경로/디렉토리
    키워드를 추출해 "허용된 범위(allowed scope)"로 삼는다.
  - 실제로 변경/생성된 파일 목록(git diff --name-only 결과 또는
    harness가 추출한 artifact 파일 목록)과 대조한다.
  - 허용 범위에 없는 파일이 섞여 있으면 "scope drift"로 플래그하고,
    구체적으로 어떤 파일이 왜 범위 밖인지 사유를 함께 반환한다.

이건 LLM 판단을 대체하는 게 아니라 보완하는 안전망이다 — 명시적으로 언급된
디렉토리/파일 밖의 변경은 "의심 신호"로만 취급하고 최종 판단은 리뷰어(LLM
또는 사람)에게 맡긴다. False positive를 줄이기 위해 다음을 허용 범위에
자동 포함한다: 테스트 파일(언급된 소스파일과 짝을 이루는 test_*/*_test 파일),
설정/문서 변경이 명시적으로 요청에 포함된 경우.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path, PurePosixPath


@dataclasses.dataclass
class ScopeDriftResult:
    """스코프 드리프트 검사 결과."""
    in_scope: list[str]
    out_of_scope: list[str]
    allowed_patterns: list[str]
    has_drift: bool


# task 설명에서 파일 경로처럼 보이는 토큰을 추출하는 패턴.
# 예: "nova/kernel/interrupt.py", "tests/unit/test_foo.py", "harnesses/qa/"
#
# 확장자 뒤에 \b(단어 경계)를 쓰지 않는다 — 한국어 문자(가-힣)는 Python
# re 모듈에서 \w에 포함되므로 "interrupt.py에"처럼 확장자 바로 뒤에 한글
# 조사가 붙는 매우 흔한 케이스에서 \b가 걸리지 않아 매칭에 실패한다.
# 대신 부정 전방탐색으로 "다음 글자가 영숫자/./_/- 가 아니면 경계로 인정"
# 방식을 쓴다 (한글 조사, 공백, 구두점 모두 정상적으로 경계로 처리됨).
_PATH_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_\-./]*\.(?:py|yaml|yml|json|md|txt|sh|toml|cfg|ini)"
    r"(?![A-Za-z0-9_.\-])"
    r"|[A-Za-z0-9_][A-Za-z0-9_\-./]*/(?![A-Za-z0-9_.\-])"
)


def extract_allowed_scope(task_description: str) -> list[str]:
    """task 설명 텍스트에서 파일/디렉토리 경로 힌트를 추출.

    Returns
    -------
    list[str]
        정규화된 경로 프리픽스 목록. 예: ["nova/kernel/interrupt.py",
        "tests/unit/"]. 아무 것도 못 찾으면 빈 리스트(이 경우 스코프
        드리프트 검사는 스킵하는 것이 안전 — 모든 파일을 밖으로 오판하지
        않도록).
    """
    if not task_description:
        return []
    tokens = _PATH_TOKEN_RE.findall(task_description)
    normalized = []
    for t in tokens:
        t = t.strip().strip(".,;:()[]{}\"'")
        if not t:
            continue
        # 절대경로/상위경로 탈출 방지 — 상대경로 힌트만 사용
        if t.startswith("/") or ".." in t:
            continue
        normalized.append(t)
    # 중복 제거, 순서 보존
    seen = set()
    result = []
    for t in normalized:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _is_test_pair(changed_file: str, source_file: str) -> bool:
    """changed_file이 source_file의 테스트 파일로 보이는지 (스코프 허용 대상)."""
    src_stem = PurePosixPath(source_file).stem
    changed_name = PurePosixPath(changed_file).name
    if changed_name in (f"test_{src_stem}.py", f"{src_stem}_test.py"):
        return True
    return bool(src_stem) and src_stem in changed_name and "test" in changed_name.lower()


def check_scope_drift(
    task_description: str,
    changed_files: list[str],
) -> ScopeDriftResult:
    """실제 변경파일 목록을 task 설명에서 추출한 허용범위와 대조.

    Parameters
    ----------
    task_description:
        요구사항/분석 텍스트 (harness의 analysis.md 등).
    changed_files:
        실제로 생성/수정된 파일 경로 목록 (git diff --name-only 또는
        harness가 추출한 artifact 목록).

    Returns
    -------
    ScopeDriftResult
        allowed_patterns가 비어있으면(task에서 아무 경로 힌트도 못 찾음)
        has_drift는 항상 False — 판단 근거가 없을 때 오탐으로 차단하지
        않는 보수적 정책.
    """
    allowed = extract_allowed_scope(task_description)

    if not allowed:
        return ScopeDriftResult(
            in_scope=list(changed_files),
            out_of_scope=[],
            allowed_patterns=[],
            has_drift=False,
        )

    in_scope: list[str] = []
    out_of_scope: list[str] = []

    for cf in changed_files:
        cf_norm = cf.lstrip("./")
        matched = False
        for pattern in allowed:
            pat_norm = pattern.lstrip("./")
            if pat_norm.endswith("/"):
                # 디렉토리 힌트: prefix 매칭
                if cf_norm.startswith(pat_norm):
                    matched = True
                    break
            else:
                # 파일 힌트: 정확 매칭 또는 같은 파일명
                if cf_norm == pat_norm or PurePosixPath(cf_norm).name == PurePosixPath(pat_norm).name:
                    matched = True
                    break
                if _is_test_pair(cf_norm, pat_norm):
                    matched = True
                    break
        if matched:
            in_scope.append(cf)
        else:
            out_of_scope.append(cf)

    return ScopeDriftResult(
        in_scope=in_scope,
        out_of_scope=out_of_scope,
        allowed_patterns=allowed,
        has_drift=len(out_of_scope) > 0,
    )
