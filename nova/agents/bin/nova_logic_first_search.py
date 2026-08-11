#!/usr/bin/env python3
"""
nova_logic_first_search.py — Logic-First 검색 레이어 (Graph Engineering Step 9)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
원문 원칙 (@unicodef1wn):
  "Finding doesn't need intelligence. It's a matching problem — shouldn't cost a model call."
  Steps 1~5: Logic (no LLM) → Step 6: LLM 1회 (evidence 준비된 상태에서)

실행:
  from nova_logic_first_search import logic_first_search
  results = logic_first_search("nova eval engineering 갭")
  # → {"lane": "direct"|"graph_expand"|"web", "pages": [...], "top_score": 0.87}
"""
import re, os, sys
from pathlib import Path
from typing import NamedTuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))
KB_DIR      = HERMES_HOME / "kb"

# LangGraph 스타일 라우팅 임계값
THRESHOLD_DIRECT       = 0.8   # 즉시 답변 (1~2 파일)
THRESHOLD_GRAPH_EXPAND = 0.5   # edge 순회 후 재검색
# 그 미만 → "web" (외부 검색)

# 불용어 (한국어 + 영어 공통)
STOPWORDS = {
    "이", "가", "은", "는", "을", "를", "에", "의", "로", "와", "과",
    "도", "에서", "으로", "이다", "있다", "없다", "하다", "되다",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "and", "or", "of", "in", "to", "for", "with", "on", "at",
}

class SearchResult(NamedTuple):
    path: str
    title: str
    score: float
    snippet: str
    page_id: str


def _extract_keywords(query: str) -> list[str]:
    """Step 1: 키워드 추출 — LLM 없이, 불용어 제거 + 형태소 단순화"""
    # 한국어/영어 토큰 분리
    tokens = re.findall(r'[가-힣]+|[a-zA-Z0-9_\-]+', query.lower())
    # 불용어 제거 + 1글자 이하 제거
    keywords = [t for t in tokens if t not in STOPWORDS and len(t) > 1]
    return keywords


def _score_index_line(line: str, keywords: list[str]) -> float:
    """Step 2: index.md 1줄 스코어링 — 파일 열지 않고"""
    if not keywords:
        return 0.0
    line_low = line.lower()
    hits = sum(1 for kw in keywords if kw in line_low)
    # 완전 일치 보너스
    bonus = 0.2 if any(kw in line for kw in keywords) else 0.0
    return min(1.0, hits / len(keywords) + bonus)


def _load_index_lines(index_path: Path) -> list[tuple[str, str, str]]:
    """index.md → [(wikilink, title_hint, line)] 파싱"""
    if not index_path.exists():
        return []
    entries = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        # [[projects/foo]] 또는 [[foo]] 형식
        m = re.search(r'\[\[([^\]]+)\]\]', line)
        if m:
            wikilink = m.group(1).strip()
            title_hint = re.sub(r'\[\[.*?\]\]', '', line).strip(" -—|")
            entries.append((wikilink, title_hint, line))
    return entries


def _resolve_path(wikilink: str) -> Path | None:
    """wikilink → 실제 파일 경로"""
    candidates = [
        KB_DIR / f"{wikilink}.md",
        HERMES_HOME / f"{wikilink}.md",
        KB_DIR / f"projects/{wikilink}.md",
        KB_DIR / f"config/{wikilink}.md",
        KB_DIR / f"fixes/{wikilink}.md",
    ]
    for c in candidates:
        if c.exists():
            return c
    # stem 매칭
    stem = Path(wikilink).stem
    for md in KB_DIR.rglob(f"{stem}.md"):
        return md
    return None


def _extract_section(content: str, keywords: list[str], max_chars: int = 600) -> str:
    """Step 4: 해당 섹션만 읽기 — 전체 파일 아님"""
    lines = content.splitlines()
    best_start = 0
    best_hits = 0
    # 키워드가 가장 많이 등장하는 섹션 찾기 (50줄 윈도우)
    window = 50
    for i in range(0, max(1, len(lines) - window + 1), 10):
        chunk = "\n".join(lines[i:i+window]).lower()
        hits = sum(chunk.count(kw) for kw in keywords)
        if hits > best_hits:
            best_hits = hits
            best_start = i
    snippet_lines = lines[best_start:best_start+window]
    snippet = "\n".join(snippet_lines)[:max_chars]
    return snippet


def _follow_edges(page_id: str, depth: int = 1) -> list[str]:
    """Step 5: edge 1개 follow — knowledge_graph_edges 순회"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(NOVA_HOME / "brain.db"), timeout=5)
        # referenced_by/depends_on 순서로 edge 탐색
        rows = conn.execute(
            "SELECT dst_page_id, edge_type, weight FROM knowledge_graph_edges "
            "WHERE src_page_id=? ORDER BY weight DESC LIMIT 3",
            [page_id]
        ).fetchall()
        # dst page_id → path
        neighbor_ids = [r[0] for r in rows]
        if not neighbor_ids:
            conn.close()
            return []
        placeholders = ",".join("?" * len(neighbor_ids))
        paths = conn.execute(
            f"SELECT path FROM pages WHERE id IN ({placeholders})",
            neighbor_ids
        ).fetchall()
        conn.close()
        return [r[0] for r in paths]
    except Exception:
        return []


def _get_page_id(path_rel: str) -> str | None:
    """pages 테이블에서 page_id 조회"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(NOVA_HOME / "brain.db"), timeout=5)
        row = conn.execute("SELECT id FROM pages WHERE path=?", [path_rel]).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def logic_first_search(query: str, top_k: int = 3) -> dict:
    """
    Logic-First 검색 (LLM 없음):
      Step1: 키워드 추출
      Step2: index.md 스코어링 (파일 열지 않음)
      Step3: 상위 1~2개만 선택
      Step4: 해당 섹션만 읽기
      Step5: edge 1개 follow (필요 시)
    → {"lane": str, "top_score": float, "pages": [SearchResult], "expanded": [str]}
    """
    # Step 1
    keywords = _extract_keywords(query)
    if not keywords:
        return {"lane": "web", "top_score": 0.0, "pages": [], "expanded": []}

    # Step 2: index.md 스코어링
    index_path = KB_DIR / "index.md"
    entries = _load_index_lines(index_path)

    scored = []
    for wikilink, title_hint, line in entries:
        score = _score_index_line(line, keywords)
        if score > 0:
            scored.append((score, wikilink, title_hint))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score = scored[0][0] if scored else 0.0

    # Step 3: 라우팅 결정
    if top_score >= THRESHOLD_DIRECT:
        lane = "direct"
        n_open = 1
    elif top_score >= THRESHOLD_GRAPH_EXPAND:
        lane = "graph_expand"
        n_open = 2
    else:
        lane = "web"
        return {"lane": "web", "top_score": top_score, "pages": [], "expanded": [], "keywords": keywords}

    # Step 4: 상위 n개 파일만 열어 섹션 추출
    results = []
    for score, wikilink, title_hint in scored[:n_open]:
        fpath = _resolve_path(wikilink)
        if not fpath or not fpath.exists():
            continue
        content = fpath.read_text(encoding="utf-8", errors="replace")
        snippet = _extract_section(content, keywords)
        rel_path = str(fpath.relative_to(HERMES_HOME)) if fpath.is_relative_to(HERMES_HOME) else str(fpath)
        page_id = _get_page_id(rel_path) or ""
        results.append(SearchResult(
            path=str(fpath),
            title=title_hint[:80],
            score=score,
            snippet=snippet,
            page_id=page_id,
        ))

    # Step 5: graph_expand 시 edge 1개 follow
    expanded = []
    if lane == "graph_expand" and results:
        top_page_id = results[0].page_id
        if top_page_id:
            neighbor_paths = _follow_edges(top_page_id, depth=1)
            expanded = neighbor_paths[:2]  # 최대 2개

    return {
        "lane": lane,
        "top_score": top_score,
        "pages": [r._asdict() for r in results],
        "expanded": expanded,
        "keywords": keywords,
    }


if __name__ == "__main__":
    import json
    query = " ".join(sys.argv[1:]) or "nova eval engineering gap faithfulness"
    result = logic_first_search(query)
    print(f"Lane: {result['lane']} | Top score: {result['top_score']:.2f}")
    print(f"Keywords: {result['keywords']}")
    print(f"Pages found: {len(result['pages'])}")
    for p in result["pages"]:
        print(f"  [{p['score']:.2f}] {p['path']}")
        print(f"       {p['snippet'][:120]}...")
    if result["expanded"]:
        print(f"Edge-expanded: {result['expanded']}")
