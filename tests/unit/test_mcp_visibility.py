"""
tests/unit/test_mcp_visibility.py — MCP 노출 화이트리스트 정밀검증 (2026-08-28)

핵심 검증 목표: brain.db를 MCP로 노출하기로 결정했을 때, 명시적으로
"mcp:public" 태그가 붙지 않은 page/take는 절대로 새어나가지 않는다는 것을
증명한다. 이 테스트가 실패하면 사용자의 실제 사내/개인 정보가 다른
로컬 에이전트(Claude Code, Codex 등)에 노출될 수 있으므로, 이 파일의
테스트는 "일반적인 회귀 방지" 이상의 보안 경계선이다.
"""
from __future__ import annotations

from nova.kernel.mcp_visibility import (
    MCP_PUBLIC_TAG,
    filter_visible_pages,
    filter_visible_takes,
    is_mcp_visible,
)


def test_no_tags_is_hidden_by_default():
    """가장 중요한 테스트: 태그가 전혀 없으면 반드시 숨겨진다."""
    decision = is_mcp_visible(None)
    assert decision.visible is False


def test_empty_string_tags_is_hidden():
    decision = is_mcp_visible("")
    assert decision.visible is False


def test_unrelated_tags_are_hidden():
    """다른 태그가 아무리 많아도 mcp:public이 없으면 숨겨진다."""
    decision = is_mcp_visible("project, important, archive, reviewed")
    assert decision.visible is False


def test_explicit_public_tag_is_visible():
    decision = is_mcp_visible(f"project, {MCP_PUBLIC_TAG}")
    assert decision.visible is True


def test_public_tag_alone_is_visible():
    decision = is_mcp_visible(MCP_PUBLIC_TAG)
    assert decision.visible is True


def test_similar_but_not_exact_tag_is_hidden():
    """부분 문자열 매치로 오탐하지 않는지 확인 (예: 'mcp:public-ish' 같은
    태그가 실수로 통과되면 안 됨)."""
    decision = is_mcp_visible("mcp:public-ish")
    assert decision.visible is False
    decision2 = is_mcp_visible("not-mcp:public")
    assert decision2.visible is False


def test_filter_visible_pages_default_deny():
    """실제 사내정보를 흉내낸 페이지들이 태그 없이는 전부 걸러지는지 확인."""
    pages = [
        {"id": "p1", "title": "MMS_PT2 화성 세타공장 분석", "tags": None},
        {"id": "p2", "title": "사내 서버 접속정보", "tags": "config"},
        {"id": "p3", "title": "공개 가능한 일반 지식", "tags": f"knowledge, {MCP_PUBLIC_TAG}"},
    ]
    visible = filter_visible_pages(pages)
    assert len(visible) == 1
    assert visible[0]["id"] == "p3"
    assert "_mcp_visibility_reason" in visible[0]


def test_filter_visible_pages_empty_input():
    assert filter_visible_pages([]) == []


def test_filter_visible_pages_all_hidden_when_none_tagged():
    pages = [{"id": f"p{i}", "tags": None} for i in range(50)]
    assert filter_visible_pages(pages) == []


def test_filter_visible_takes_inherits_from_parent_page():
    page_tags = {
        "page1": f"{MCP_PUBLIC_TAG}",
        "page2": "private-stuff",
    }
    takes = [
        {"id": "t1", "page_id": "page1", "claim": "공개 가능"},
        {"id": "t2", "page_id": "page2", "claim": "비공개 정보"},
    ]
    visible = filter_visible_takes(takes, page_tags)
    assert len(visible) == 1
    assert visible[0]["id"] == "t1"


def test_filter_visible_takes_orphan_take_defaults_to_hidden():
    """page_id가 매핑에 없는 take(고아 데이터)는 안전하게 숨김 처리."""
    takes = [{"id": "t1", "page_id": "nonexistent_page", "claim": "x"}]
    visible = filter_visible_takes(takes, page_tags_by_id={})
    assert visible == []


def test_filter_visible_takes_missing_page_id_field():
    """page_id 필드 자체가 없는 손상된 take도 안전하게 숨김."""
    takes = [{"id": "t1", "claim": "no page_id key"}]
    visible = filter_visible_takes(takes, page_tags_by_id={"whatever": MCP_PUBLIC_TAG})
    assert visible == []
