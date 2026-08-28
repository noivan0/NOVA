"""
nova.kernel.mcp_visibility — Opt-in whitelist for exposing brain.db over MCP
================================================================================

배경(2026-08-28): NOVA를 MCP(Model Context Protocol) 서버로 노출해 Claude
Code/Codex 등 다른 에이전트가 brain.db를 조회할 수 있게 하자는 제안에
사용자가 "그러면 내 brain.db 정보를 외부 사람이 볼 수 있는 거 아니냐"고
정당하게 우려를 제기했다.

실측 확인 결과: 이 사용자의 brain.db(2059 pages)에는 실제 회사 업무
데이터(사내 시스템명, 공장/설비 코드명 등)가 수백 건 단위로 포함되어
있다. 이건 브레인이 정상적으로 실사용되어 왔다는 증거이자, 동시에
"기본값 = 전부 노출"은 절대 안전하지 않다는 뜻이다.

이 모듈은 **화이트리스트(opt-in) 전용** 정책을 강제한다:
  - 어떤 page도 명시적으로 허용 표시가 없으면 절대 노출되지 않는다
    (블랙리스트/키워드 필터링은 의도적으로 채택하지 않음 — 새 사내 코드명이
    생길 때마다 필터를 갱신해야 하는 블랙리스트는 필연적으로 새는 것을
    막을 수 없다. 화이트리스트는 실수로 노출되는 방향이 아니라 실수로
    숨겨지는 방향으로만 실패한다 — 훨씬 안전한 실패 모드).
  - 허용 표시는 page.tags에 "mcp:public" 태그를 명시적으로 붙인 경우에만
    유효하다. 사용자가 "이 페이지는 공유해도 된다"고 한 건 한 건 확인한
    것만 노출.
  - MCP 서버 자체는 항상 stdio(로컬 프로세스 파이프)로만 동작한다 —
    네트워크 포트를 여는 http 모드는 이 모듈/서버 어디에도 구현하지
    않는다(별도로 명시적 사용자 승인 없이는 추가하지 않을 것).
"""

from __future__ import annotations

import dataclasses

MCP_PUBLIC_TAG = "mcp:public"


@dataclasses.dataclass
class VisibilityDecision:
    visible: bool
    reason: str


def _parse_tags(tags_field: str | None) -> list[str]:
    """pages.tags 컬럼(콤마구분 문자열 또는 None)을 리스트로 정규화."""
    if not tags_field:
        return []
    return [t.strip() for t in tags_field.split(",") if t.strip()]


def is_mcp_visible(tags_field: str | None) -> VisibilityDecision:
    """단일 page의 tags 필드로 MCP 노출 여부를 판정.

    화이트리스트 원칙: MCP_PUBLIC_TAG가 명시적으로 없으면 항상 비공개.
    """
    tags = _parse_tags(tags_field)
    if MCP_PUBLIC_TAG in tags:
        return VisibilityDecision(visible=True, reason=f"tagged '{MCP_PUBLIC_TAG}'")
    return VisibilityDecision(
        visible=False,
        reason=f"no '{MCP_PUBLIC_TAG}' tag (opt-in whitelist — default is hidden)",
    )


def filter_visible_pages(pages: list[dict]) -> list[dict]:
    """page dict 목록(각각 'tags' 키 보유)에서 MCP 공개 대상만 남긴다.

    입력 각 dict는 최소 {'tags': str|None, ...} 형태를 가정. 반환값은
    원본 dict에 '_mcp_visibility_reason' 키를 추가한 새 리스트 —
    감사/디버깅 시 "왜 이 페이지가 보이는지"를 항상 추적 가능하게 한다.
    """
    result = []
    for page in pages:
        decision = is_mcp_visible(page.get("tags"))
        if decision.visible:
            enriched = dict(page)
            enriched["_mcp_visibility_reason"] = decision.reason
            result.append(enriched)
    return result


def filter_visible_takes(takes: list[dict], page_tags_by_id: dict[str, str | None]) -> list[dict]:
    """takes 목록을 각자의 page_id가 가리키는 page의 tags로 필터링.

    takes 테이블 자체에는 tags가 없으므로 (id -> page_id -> page.tags)
    경로로 부모 page의 공개여부를 물려받는다. page_id가 매핑 테이블에
    없으면(고아 take) 안전하게 비공개로 처리한다.
    """
    result = []
    for take in takes:
        page_id = take.get("page_id")
        tags = page_tags_by_id.get(page_id) if page_id else None
        decision = is_mcp_visible(tags)
        if decision.visible:
            enriched = dict(take)
            enriched["_mcp_visibility_reason"] = decision.reason
            result.append(enriched)
    return result
