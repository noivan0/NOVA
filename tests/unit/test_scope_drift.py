"""
tests/unit/test_scope_drift.py — nova.kernel.scope_drift 정밀검증 (gstack parity, 2026-08-28)
"""
from __future__ import annotations

from nova.kernel.scope_drift import ScopeDriftResult, check_scope_drift, extract_allowed_scope


def test_extract_allowed_scope_finds_file_paths():
    task = "nova/kernel/interrupt.py에 새 메서드를 추가하고 문서화한다."
    scope = extract_allowed_scope(task)
    assert "nova/kernel/interrupt.py" in scope


def test_extract_allowed_scope_handles_korean_particle_after_extension():
    """한글 조사가 확장자 바로 뒤에 붙는 흔한 케이스(...py에, ...py를 등)."""
    for suffix in ["에", "를", "은", "이", "만"]:
        task = f"nova/kernel/interrupt.py{suffix} 수정해줘"
        scope = extract_allowed_scope(task)
        assert "nova/kernel/interrupt.py" in scope, f"실패: suffix={suffix!r}, scope={scope}"


def test_extract_allowed_scope_finds_directory_hints():
    task = "tests/unit/ 아래에 테스트를 추가한다."
    scope = extract_allowed_scope(task)
    assert "tests/unit/" in scope


def test_extract_allowed_scope_multiple_files():
    task = "a/b.py와 c/d.yaml 둘 다 수정, 그리고 e/f.md 문서도 갱신."
    scope = extract_allowed_scope(task)
    assert "a/b.py" in scope
    assert "c/d.yaml" in scope
    assert "e/f.md" in scope


def test_extract_allowed_scope_empty_when_no_paths_mentioned():
    task = "버그를 고쳐줘, 왜 안 되는지 모르겠어."
    scope = extract_allowed_scope(task)
    assert scope == []


def test_extract_allowed_scope_rejects_path_traversal_and_absolute():
    task = "/etc/passwd나 ../../secret.py 를 건드리지 마세요."
    scope = extract_allowed_scope(task)
    assert "/etc/passwd" not in scope
    assert not any(".." in s for s in scope)


# ── check_scope_drift() ──────────────────────────────────────────────────────

def test_check_scope_drift_no_drift_when_all_files_in_scope():
    task = "nova/kernel/interrupt.py와 tests/unit/test_interrupt.py를 수정."
    result = check_scope_drift(task, [
        "nova/kernel/interrupt.py",
        "tests/unit/test_interrupt.py",
    ])
    assert isinstance(result, ScopeDriftResult)
    assert result.has_drift is False
    assert result.out_of_scope == []


def test_check_scope_drift_flags_out_of_scope_files():
    task = "nova/kernel/interrupt.py만 수정한다."
    result = check_scope_drift(task, [
        "nova/kernel/interrupt.py",
        "nova/providers/llm.py",   # 언급 안 됨
        "README.md",               # 언급 안 됨
    ])
    assert result.has_drift is True
    assert "nova/providers/llm.py" in result.out_of_scope
    assert "README.md" in result.out_of_scope
    assert "nova/kernel/interrupt.py" in result.in_scope


def test_check_scope_drift_allows_test_pair_for_mentioned_source():
    """언급된 소스파일의 테스트 파일은 자동으로 범위 내로 인정."""
    task = "nova/kernel/careful.py를 구현한다."
    result = check_scope_drift(task, [
        "nova/kernel/careful.py",
        "tests/unit/test_careful.py",  # 짝지어진 테스트, drift 아님
    ])
    assert result.has_drift is False


def test_check_scope_drift_directory_hint_covers_nested_files():
    task = "tests/unit/ 에 새 테스트들을 추가."
    result = check_scope_drift(task, [
        "tests/unit/test_a.py",
        "tests/unit/test_b.py",
    ])
    assert result.has_drift is False


def test_check_scope_drift_conservative_when_no_scope_hint():
    """task에 아무 경로 힌트도 없으면 drift 판정을 내리지 않는다 (오탐 방지)."""
    result = check_scope_drift("아무 파일이나 고쳐도 돼", ["any/random/file.py"])
    assert result.has_drift is False
    assert result.allowed_patterns == []


def test_check_scope_drift_empty_changed_files():
    task = "nova/kernel/interrupt.py 수정."
    result = check_scope_drift(task, [])
    assert result.has_drift is False
    assert result.in_scope == []
    assert result.out_of_scope == []
