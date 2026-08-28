"""
nova/mcp/server.py — NOVA brain.db MCP server (stdio-only, opt-in whitelist)
================================================================================

배경(2026-08-28): brain.db를 다른 로컬 에이전트(Claude Code, Codex 등)와
공유하고 싶다는 요구에서 출발했으나, 사용자가 "그럼 내 brain.db 정보를
외부 사람이 볼 수 있는 거 아니냐"고 정당하게 우려를 제기했다. 실측 결과
이 사용자의 brain.db(2059 pages)에는 사내 시스템/공정명이 수백 건
포함되어 있음을 확인했다.

이 서버는 두 가지 안전장치를 하드코딩한다:

1. **stdio 전용, 네트워크 미노출.** `run_stdio_async()`만 사용한다.
   SSE나 streamable-HTTP transport(네트워크 리스너를 여는 방식)는
   의도적으로 이 파일 어디에서도 호출하지 않는다 — 이 서버는 로컬
   클라이언트(Claude Code 등)가 서브프로세스로 직접 띄우고 stdin/stdout
   파이프로만 통신한다. 인터넷 어디에서도 접근 불가능하며, 이걸 벗어나려면
   파일을 새로 작성해야 한다(실수로 네트워크에 열릴 수 없는 구조).

2. **화이트리스트(opt-in) 필터링.** nova.kernel.mcp_visibility의 원칙
   (명시적으로 "mcp:public" 태그가 붙은 page/take만 노출)을 모든 tool
   핸들러에 강제한다. 이 서버가 노출하는 두 tool
   (nova_kb_search_public / nova_kb_list_public)은 SQL 쿼리 단계에서부터
   tags 컬럼에 정확한 토큰 매치 조건을 걸어, 애플리케이션 레이어의 필터
   버그가 있어도 DB 레벨에서 다시 막힌다(defense in depth).

사용법 (클라이언트 측, 로컬 stdio 등록):
    claude mcp add nova -- python3 -m nova.mcp.server --brain-db ~/.nova/brain.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from nova.kernel.mcp_visibility import MCP_PUBLIC_TAG


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """brain.db에 읽기전용으로 연결. 실수 쓰기를 원천 차단."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sql_public_filter() -> str:
    """SQL WHERE 절 조각 — 화이트리스트 태그가 정확히 일치하는 page만 통과.

    nova.kernel.mcp_visibility.is_mcp_visible()과 반드시 동일한 정책을
    유지해야 한다(app-layer와 DB-layer 이중 방어). 쉼표 구분 토큰 중 하나가
    정확히 일치해야 하므로, 'mcp:public-ish' 같은 유사 태그의 오탐을
    방지하기 위해 구분자를 포함한 다중 LIKE 패턴을 사용한다.
    """
    tag = MCP_PUBLIC_TAG
    return (
        f"(tags = '{tag}' OR tags LIKE '{tag},%' OR tags LIKE '%, {tag}' "
        f"OR tags LIKE '%, {tag},%' OR tags LIKE '%,{tag}' OR tags LIKE '%,{tag},%')"
    )


def search_public_kb(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """mcp:public 태그가 붙은 page만 대상으로 키워드 검색."""
    sql = f"""
        SELECT id, path, title, summary, tags
        FROM pages
        WHERE {_sql_public_filter()}
          AND (title LIKE ? OR summary LIKE ? OR compiled_truth LIKE ?)
        LIMIT ?
    """
    like = f"%{query}%"
    rows = conn.execute(sql, (like, like, like, limit)).fetchall()
    return [dict(r) for r in rows]


def list_public_pages(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """mcp:public 태그가 붙은 page 전체 목록 (내용 없이 메타데이터만)."""
    sql = f"""
        SELECT id, path, title, tags
        FROM pages
        WHERE {_sql_public_filter()}
        ORDER BY updated_at DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def build_server(brain_db_path: str) -> MCPServer:
    server = MCPServer(
        "nova-brain",
        instructions=(
            "Read-only access to the NOVA knowledge base — but ONLY pages the user "
            f"explicitly tagged '{MCP_PUBLIC_TAG}'. Nothing else in the user's "
            "brain.db is ever returned."
        ),
    )
    conn = _connect_readonly(brain_db_path)

    @server.tool(
        name="nova_kb_search_public",
        description=(
            "Search ONLY the NOVA knowledge-base pages the user has explicitly "
            f"marked shareable (tagged '{MCP_PUBLIC_TAG}'). Everything else in "
            "the user's brain.db is never returned by this tool, by design."
        ),
    )
    def nova_kb_search_public(query: str, limit: int = 10) -> str:
        results = search_public_kb(conn, query, limit)
        return json.dumps(results, ensure_ascii=False, indent=2)

    @server.tool(
        name="nova_kb_list_public",
        description=(
            f"List NOVA knowledge-base pages tagged '{MCP_PUBLIC_TAG}' (explicitly "
            "marked shareable by the user). Does not list private pages."
        ),
    )
    def nova_kb_list_public(limit: int = 50) -> str:
        results = list_public_pages(conn, limit)
        return json.dumps(results, ensure_ascii=False, indent=2)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NOVA brain.db MCP server (stdio-only, opt-in whitelist — see module docstring)"
    )
    parser.add_argument(
        "--brain-db",
        default=str(Path.home() / ".nova" / "brain.db"),
        help="Path to brain.db (default: ~/.nova/brain.db)",
    )
    args = parser.parse_args()

    server = build_server(args.brain_db)
    # 의도적으로 stdio transport만 사용한다 — SSE/streamable-HTTP
    # (네트워크 리스너)는 이 파일에서 절대 호출하지 않는다. 네트워크
    # 노출이 필요해지면 별도 명시적 승인과 별도 파일로 분리해야 한다.
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
