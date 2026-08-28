"""
tests/unit/test_mcp_server.py — NOVA MCP 서버 화이트리스트/SQL 안전성 검증 (2026-08-28)

이 테스트 파일은 사용자가 제기한 정당한 우려("MCP로 열면 내 brain.db
정보를 외부 사람이 볼 수 있는 거 아니냐")에 대한 실증적 답변이다.
검증 목표:
  1. 태그 없는(기본) page는 search/list 어느 쪽으로도 절대 나오지 않는다
  2. mcp:public 태그가 붙은 page만 정확히 나온다
  3. SQL 인젝션 시도가 파라미터 바인딩으로 무력화된다
  4. 서버 소스 어디에도 네트워크 transport(sse/streamable-http) 호출이
     없다 — 이건 코드 정적 검사로, "안전하다고 주장"이 아니라 "실제로
     그 코드가 존재하지 않음"을 grep으로 증명한다
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from nova.kernel.mcp_visibility import MCP_PUBLIC_TAG
from nova.mcp.server import list_public_pages, search_public_kb


def _make_test_db(tmp_path: Path) -> str:
    """최소 스키마의 격리된 테스트용 brain.db 생성 (실제 사용자 DB 절대 미접근)."""
    db_path = str(tmp_path / "test_brain.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE pages (
            id TEXT PRIMARY KEY,
            path TEXT,
            title TEXT,
            summary TEXT,
            compiled_truth TEXT,
            tags TEXT,
            updated_at TEXT
        )
    """)
    rows = [
        ("p1", "kb/internal/hmg-secret.md", "사내 시스템 접속정보", "민감정보", "", None, "2026-01-01"),
        ("p2", "kb/projects/public-project.md", "공개 프로젝트 노트", "공유 가능", "", MCP_PUBLIC_TAG, "2026-01-02"),
        ("p3", "kb/lessons/private-lesson.md", "개인 교훈", "비공개", "", "archive", "2026-01-03"),
        ("p4", "kb/mixed.md", "여러 태그 중 public 포함", "섞인 태그", "", f"project, {MCP_PUBLIC_TAG}, reviewed", "2026-01-04"),
        ("p5", "kb/fake-public.md", "유사 태그 오탐 방지 확인용", "가짜", "", "mcp:public-ish", "2026-01-05"),
    ]
    conn.executemany(
        "INSERT INTO pages (id, path, title, summary, compiled_truth, tags, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def readonly_conn(tmp_path):
    db_path = _make_test_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def test_list_public_pages_excludes_untagged(readonly_conn):
    results = list_public_pages(readonly_conn)
    paths = [r["path"] for r in results]
    assert "kb/internal/hmg-secret.md" not in paths, "태그 없는 민감 페이지가 노출됨!"
    assert "kb/lessons/private-lesson.md" not in paths, "다른 태그만 있는 페이지가 노출됨!"


def test_list_public_pages_includes_exact_tag(readonly_conn):
    results = list_public_pages(readonly_conn)
    paths = [r["path"] for r in results]
    assert "kb/projects/public-project.md" in paths


def test_list_public_pages_includes_mixed_tags_with_public(readonly_conn):
    results = list_public_pages(readonly_conn)
    paths = [r["path"] for r in results]
    assert "kb/mixed.md" in paths


def test_list_public_pages_excludes_similar_but_wrong_tag(readonly_conn):
    """'mcp:public-ish' 같은 유사 태그는 통과하면 안 된다 (SQL LIKE 오탐 방지)."""
    results = list_public_pages(readonly_conn)
    paths = [r["path"] for r in results]
    assert "kb/fake-public.md" not in paths


def test_list_public_pages_exact_count():
    """정확히 2개(p2, p4)만 공개되어야 한다 — 5개 중 3개는 반드시 숨겨짐."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_test_db(Path(td))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        results = list_public_pages(conn)
        assert len(results) == 2


def test_search_public_kb_excludes_untagged_even_if_query_matches(readonly_conn):
    """검색어가 태그없는 민감페이지 제목과 일치해도 절대 반환되지 않는다."""
    results = search_public_kb(readonly_conn, "사내")
    assert results == []


def test_search_public_kb_finds_tagged_page(readonly_conn):
    results = search_public_kb(readonly_conn, "공개")
    paths = [r["path"] for r in results]
    assert "kb/projects/public-project.md" in paths


def test_search_public_kb_sql_injection_is_neutralized(readonly_conn):
    """악의적 입력이 파라미터 바인딩으로 무력화되고, 정상적으로 빈 결과를 반환."""
    malicious = "'; DROP TABLE pages; --"
    results = search_public_kb(readonly_conn, malicious)
    assert results == []
    # 인젝션이 실패했다면 테이블이 여전히 조회 가능해야 함
    count = readonly_conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    assert count == 5


def test_readonly_connection_rejects_writes(readonly_conn):
    """query_only=ON 연결에서는 실수로도 쓰기가 불가능해야 한다."""
    with pytest.raises(sqlite3.OperationalError):
        readonly_conn.execute("INSERT INTO pages (id, path) VALUES ('x', 'y')")


def test_server_source_has_no_network_transport_calls():
    """정적 코드 검사: 서버 소스 파일 어디에도 실제 run_sse_async /
    run_streamable_http_async 호출(설명 주석이 아니라 실행 코드)이 없어야
    한다. 이건 '안전하다고 주장'이 아니라 코드가 존재하지 않음을 증명."""
    import inspect
    import nova.mcp.server as server_module

    source = inspect.getsource(server_module)
    # 함수 호출 형태(괄호 포함)로만 검색 — 주석 속 언급과 구분
    assert "run_sse_async()" not in source
    assert "run_streamable_http_async()" not in source
    assert 'transport="sse"' not in source
    assert 'transport="streamable-http"' not in source


def test_build_server_uses_default_stdio_transport():
    """main()이 run()을 호출할 때 transport 인자를 넘기지 않거나 'stdio'를
    명시하는지 소스에서 확인 (기본값이 stdio이므로 인자 생략도 안전)."""
    import inspect
    import nova.mcp.server as server_module

    source = inspect.getsource(server_module.main)
    assert 'transport="stdio"' in source or "server.run()" in source
