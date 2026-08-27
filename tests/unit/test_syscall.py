"""
test_syscall.py — NOVA Kernel KernelAPI 단위 테스트

8개 필수 케이스 전부 포함:
  1. nova-dev → workspace/code_implement/ 쓰기 성공
  2. nova-dev → workspace/research/ 쓰기 실패 (NovaPermissionError)
  3. nova-dev → kb/ 쓰기 실패 (NovaPermissionError)
  4. harness → workspace/** 쓰기 성공
  5. 모든 에이전트 → 읽기 성공
  6. kb_write 시 id 자동 생성 (id=NULL 불가)
  7. kb_write 시 agent 컬럼 자동 배정
  8. ownership.yaml 없어도 기본값으로 동작
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import os
from pathlib import Path

import pytest

from nova.kernel.syscall import KernelAPI, NovaPermissionError, NovaSyscallError
from nova.kernel.ownership import OwnershipRules


# ── 픽스처 ───────────────────────────────────────────────────────────────────

def _create_brain_db(path: str) -> None:
    """테스트용 brain.db 생성 (최소 스키마)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id              TEXT PRIMARY KEY,
            path            TEXT NOT NULL,
            title           TEXT,
            page_type       TEXT DEFAULT 'general',
            agent           TEXT,
            compiled_truth  TEXT DEFAULT '',
            content_hash    TEXT,
            char_count      INTEGER DEFAULT 0,
            indexed_at      TEXT,
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS takes (
            id          TEXT PRIMARY KEY,
            page_id     TEXT,
            kind        TEXT DEFAULT 'fact',
            holder      TEXT,
            claim       TEXT NOT NULL,
            weight      REAL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def brain_db(tmp_path: Path) -> str:
    """임시 brain.db 경로 반환."""
    db_path = str(tmp_path / "brain.db")
    _create_brain_db(db_path)
    return db_path


@pytest.fixture
def api(brain_db: str) -> KernelAPI:
    """기본 KernelAPI 인스턴스 (ownership.yaml 기본값 사용)."""
    return KernelAPI(brain_db=brain_db)


# ── 케이스 1: nova-dev → workspace/code_implement/ 쓰기 성공 ─────────────────

def test_case1_nova_dev_write_code_implement(api: KernelAPI, brain_db: str) -> None:
    """케이스 1: nova-dev 는 workspace/code_implement/ 에 쓰기 가능."""
    path = "workspace/code_implement/feature_x.md"
    page_id = api.kb_write(path=path, content="구현 내용", agent="nova-dev")

    assert page_id is not None
    assert len(page_id) == 16  # sha256[:16]

    # DB에 실제로 저장됐는지 확인
    conn = sqlite3.connect(brain_db)
    row = conn.execute("SELECT id FROM pages WHERE id = ?", (page_id,)).fetchone()
    conn.close()
    assert row is not None, "페이지가 DB에 저장되지 않음"


# ── 케이스 2: nova-dev → workspace/research/ 쓰기 실패 ───────────────────────

def test_case2_nova_dev_write_research_denied(api: KernelAPI) -> None:
    """케이스 2: nova-dev 는 workspace/research/ 에 쓰기 권한 없음."""
    with pytest.raises(NovaPermissionError):
        api.kb_write(
            path="workspace/research/plan.md",
            content="리서치 내용",
            agent="nova-dev",
        )


# ── 케이스 3: nova-dev → kb/ 쓰기 실패 ─────────────────────────────────────

def test_case3_nova_dev_write_kb_denied(api: KernelAPI) -> None:
    """케이스 3: nova-dev 는 kb/ 에 쓰기 권한 없음."""
    with pytest.raises(NovaPermissionError):
        api.kb_write(
            path="kb/some_topic.md",
            content="KB 내용",
            agent="nova-dev",
        )


# ── 케이스 4: harness → workspace/** 쓰기 성공 ─────────────────────────────

def test_case4_harness_write_workspace(api: KernelAPI, brain_db: str) -> None:
    """케이스 4: harness 에이전트는 workspace/** 에 쓰기 가능."""
    paths = [
        "workspace/code_implement/harness_task.md",
        "workspace/code_review/pr_100.md",
        "workspace/research/report.md",
        "workspace/misc/notes.md",
    ]
    for path in paths:
        page_id = api.kb_write(path=path, content=f"{path} 내용", agent="harness")
        assert page_id is not None, f"{path} 쓰기 실패"

    conn = sqlite3.connect(brain_db)
    count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    conn.close()
    assert count == len(paths)


# ── 케이스 5: 모든 에이전트 → 읽기 성공 ────────────────────────────────────

def test_case5_all_agents_can_read(api: KernelAPI) -> None:
    """케이스 5: 어떤 에이전트도 읽기 성공 (실제 검색 결과 없어도 예외 미발생)."""
    agents = ["nova-dev", "nova-review", "nova-research", "harness", "unknown-bot"]
    for agent in agents:
        result = api.kb_read(query="테스트 쿼리", agent=agent, limit=5)
        assert isinstance(result, list), f"{agent}: 읽기 결과가 리스트가 아님"


def test_case5_check_permission_read(api: KernelAPI) -> None:
    """케이스 5 추가: check_permission read 는 모든 에이전트 True."""
    for agent in ["nova-dev", "stranger", "harness"]:
        assert api.check_permission("some/path", agent, op="read") is True


# ── 케이스 6: kb_write 시 id 자동 생성 (NULL 불가) ─────────────────────────

def test_case6_page_id_auto_generated(api: KernelAPI, brain_db: str) -> None:
    """케이스 6: kb_write 반환값이 sha256[:16] 이고 DB id 컬럼과 일치."""
    path = "workspace/code_implement/auto_id_test.md"
    page_id = api.kb_write(path=path, content="자동 ID 테스트", agent="nova-dev")

    # 수동 계산값과 동일해야 함
    expected_id = hashlib.sha256(path.encode()).hexdigest()[:16]
    assert page_id == expected_id

    # DB에 NULL 이 아닌 id 로 저장됐는지 확인
    conn = sqlite3.connect(brain_db)
    row = conn.execute("SELECT id FROM pages WHERE id = ?", (page_id,)).fetchone()
    conn.close()
    assert row is not None
    assert row[0] is not None, "id 가 NULL 임"
    assert row[0] == expected_id


# ── 케이스 7: kb_write 시 agent 컬럼 자동 배정 ─────────────────────────────

def test_case7_agent_auto_assigned(api: KernelAPI, brain_db: str) -> None:
    """케이스 7: kb/agents/<agent-name>/ 경로에 kb_write 시 agent 자동 배정."""
    # nova-research 가 kb/agents/nova-research/ 에 쓰면
    # assigned_agent = "nova-research" (경로 추출)
    # → harness 는 workspace/** 소유자이므로 workspace에서는 harness 배정
    # kb/agents/** 는 빈 agents=[],  경로에서 추출
    path = "kb/agents/nova-research/knowledge.md"
    page_id = api.kb_write(
        path=path,
        content="리서치 지식",
        agent="nova-research",
    )

    conn = sqlite3.connect(brain_db)
    row = conn.execute(
        "SELECT agent FROM pages WHERE id = ?", (page_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    # kb/agents/nova-research/ → 추출된 agent = "nova-research"
    assert row[0] == "nova-research", f"자동 배정 agent 오류: {row[0]}"


def test_case7_agent_auto_assigned_workspace(api: KernelAPI, brain_db: str) -> None:
    """케이스 7 추가: workspace/code_implement/ 쓰기 시 nova-dev → nova-dev 배정."""
    path = "workspace/code_implement/agent_assign.md"
    page_id = api.kb_write(path=path, content="내용", agent="nova-dev")

    conn = sqlite3.connect(brain_db)
    row = conn.execute(
        "SELECT agent FROM pages WHERE id = ?", (page_id,)
    ).fetchone()
    conn.close()

    # nova-dev 는 workspace/code_implement/** 규칙의 agents 에 포함 → nova-dev 배정
    assert row[0] == "nova-dev"


# ── 케이스 8: ownership.yaml 없어도 기본값으로 동작 ──────────────────────────

def test_case8_no_yaml_fallback(brain_db: str, tmp_path: Path) -> None:
    """케이스 8: 존재하지 않는 yaml 경로를 줘도 기본값으로 동작."""
    missing_yaml = str(tmp_path / "nonexistent.yaml")
    api = KernelAPI(brain_db=brain_db, ownership_yaml=missing_yaml)

    # 기본값: allow_read=True → 읽기 성공
    result = api.kb_read(query="테스트", agent="any-agent")
    assert isinstance(result, list)

    # 기본값: allow_unknown=False → 쓰기 실패 (허용 루트 내부지만 소유권 규칙 미매칭)
    with pytest.raises(NovaPermissionError):
        api.kb_write(
            path="kb/some/path/file.md",
            content="내용",
            agent="any-agent",
        )


def test_case8_ownership_rules_no_yaml() -> None:
    """케이스 8 추가: OwnershipRules 단독으로도 yaml 없이 동작."""
    rules = OwnershipRules(yaml_path="/tmp/_nonexistent_nova_ownership.yaml")
    # 읽기 허용 (allow_read=True 기본)
    assert rules.can_read("any/path", "any-agent") is True
    # 쓰기 거부 (allow_unknown=False 기본, 폴백 ** 규칙은 read 만)
    assert rules.can_write("any/path", "any-agent") is False


# ── 추가: check_permission API 전체 확인 ────────────────────────────────────

def test_check_permission_write_and_delete(api: KernelAPI) -> None:
    """check_permission 이 write / delete / read 를 올바르게 반환."""
    # nova-dev: code_implement 쓰기 ○
    assert api.check_permission("workspace/code_implement/x.md", "nova-dev", "write") is True
    # nova-dev: research 쓰기 ✕
    assert api.check_permission("workspace/research/x.md", "nova-dev", "write") is False
    # harness: 어디든 쓰기 ○
    assert api.check_permission("workspace/research/x.md", "harness", "write") is True
    # 읽기: 누구나 ○
    assert api.check_permission("kb/secret/x.md", "stranger", "read") is True


# ── 추가: take_write / spawn 연기 테스트 ────────────────────────────────────

def test_take_write_returns_id(api: KernelAPI) -> None:
    """take_write 가 UUID 형태의 take_id 를 반환."""
    take_id = api.take_write(claim="Nova 는 항상 학습한다", kind="insight", agent="nova-research")
    assert take_id is not None
    assert len(take_id) == 36  # UUID4 길이


def test_spawn_returns_run_id(api: KernelAPI) -> None:
    """spawn 이 UUID 형태의 run_id를 가진 RunHandle을 반환."""
    handle = api.spawn(harness="nova-dev-harness", task="PR 작성", agent="nova-dev")
    assert handle is not None
    assert handle.run_id is not None
    assert len(handle.run_id) == 36
