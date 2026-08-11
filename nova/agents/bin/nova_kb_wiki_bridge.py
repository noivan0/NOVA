#!/usr/bin/env python3
"""
nova_kb_wiki_bridge.py — KB + llm-wiki 통합 인덱싱 브릿지
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

역할:
  1. KB 새 페이지 → wiki 페이지 자동 연결 (동일 주제 인덱싱)
  2. 의미적 중복 감지 (title + 핵심 키워드 기반 — LLM 없이)
  3. memory_slim 아카이브 → KB 정확 기록 → wiki 인덱스 갱신
  4. wiki index.md 자동 갱신

사용:
  python3 nova_kb_wiki_bridge.py --sync         # KB → wiki 동기화
  python3 nova_kb_wiki_bridge.py --check-dup    # 중복 페이지 감지
  python3 nova_kb_wiki_bridge.py --archive FILE # memory 아카이브 → KB+wiki 기록
"""
from __future__ import annotations

import os
import re
import sys
import json
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── 환경 ─────────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
KB_ROOT     = HERMES_HOME / "kb"
WIKI_DIR    = NOVA_HOME / "wiki"
BRAIN_DB    = NOVA_HOME / "brain.db"

LOG = NOVA_HOME / "logs" / "kb_wiki_bridge.log"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[kb-wiki-bridge] [{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 키워드 추출 (LLM 없이, 규칙 기반) ───────────────────────────────────────
STOPWORDS = {
    "the", "a", "an", "is", "in", "on", "at", "to", "of", "and", "or", "for",
    "with", "by", "from", "this", "that", "it", "we", "are", "was", "be",
    "이", "가", "을", "를", "에", "와", "과", "의", "로", "으로", "에서", "하다",
    "있다", "없다", "수", "것", "및", "등", "그", "이다", "하여", "하면",
}

def extract_keywords(text: str, top_n: int = 20) -> list[str]:
    """텍스트에서 핵심 키워드 추출 (규칙 기반)"""
    # YAML frontmatter 제거
    text = re.sub(r'^---.*?---\s*', '', text, flags=re.DOTALL)
    # 마크다운 링크/이미지 제거
    text = re.sub(r'!?\[[^\]]*\]\([^)]*\)', ' ', text)
    # 코드블록 제거
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)
    # 특수문자 → 공백
    tokens = re.findall(r'[가-힣a-zA-Z][가-힣a-zA-Z0-9_\-]{2,}', text)
    # 빈도 계산
    freq = defaultdict(int)
    for t in tokens:
        t_lower = t.lower()
        if t_lower not in STOPWORDS and len(t_lower) > 2:
            freq[t_lower] += 1
    # 상위 N개
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_n]]


def keyword_similarity(kw1: list[str], kw2: list[str]) -> float:
    """두 키워드 집합의 유사도 (Jaccard)"""
    s1, s2 = set(kw1), set(kw2)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


# ── KB 페이지 로드 ────────────────────────────────────────────────────────────
def load_kb_pages(kb_root: Path = KB_ROOT) -> list[dict]:
    """KB 마크다운 페이지 전체 로드"""
    pages = []
    if not kb_root.exists():
        return pages
    for md in kb_root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
            # frontmatter 파싱
            fm = {}
            m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if m:
                for line in m.group(1).split("\n"):
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        fm[k.strip()] = v.strip()
            title = fm.get("title", md.stem)
            agent = fm.get("agent", "unknown")
            keywords = extract_keywords(text)
            content_hash = hashlib.md5(text.encode()).hexdigest()
            # BUG-WIKI-YAML-1/4 수정 (2026-07-30/31):
            # count=1로는 첫 frontmatter만 제거 → 본문 중간 embedded frontmatter 잔존
            # (memory-archive KB 파일: 시작 frontmatter + 본문 중간에 또 다른 YAML 블록 존재)
            _t0 = re.sub(r'^---.*?---\s*', '', text, count=1, flags=re.DOTALL)
            _t1 = re.sub(r'\n---\n(?=[a-z_\'\"]).+?(?=\n---\n|\Z)', '\n', _t0, flags=re.DOTALL)
            pages.append({
                "path": md,
                "rel":  str(md.relative_to(kb_root)),
                "title": title,
                "agent": agent,
                "keywords": keywords,
                "content_hash": content_hash,
                "text_preview": _t1[:300],
            })
        except Exception as e:
            _log(f"페이지 로드 실패: {md.name} — {e}")
    return pages


# ── 의미적 중복 감지 ──────────────────────────────────────────────────────────
def find_duplicates(pages: list[dict], threshold: float = 0.55) -> list[tuple]:
    """
    의미적 중복 페이지 쌍 감지.
    threshold=0.55: 55% 이상 키워드 겹치면 중복 후보
    """
    duplicates = []
    for i, p1 in enumerate(pages):
        for p2 in pages[i+1:]:
            sim = keyword_similarity(p1["keywords"], p2["keywords"])
            if sim >= threshold:
                duplicates.append((sim, p1, p2))
    duplicates.sort(key=lambda x: -x[0])
    return duplicates


# ── wiki 페이지 생성/갱신 ─────────────────────────────────────────────────────
def kb_page_to_wiki(page: dict, wiki_dir: Path = WIKI_DIR) -> Path | None:
    """
    KB 페이지 → wiki 페이지 생성 (이미 존재하면 갱신).
    동일 title이면 내용 병합 (완전 중복은 기입 안 함).
    """
    title = page["title"]
    agent = page["agent"]
    keywords = page["keywords"][:8]
    today = datetime.now().strftime("%Y-%m-%d")

    # wiki 페이지명 결정 (title → slug)
    slug = re.sub(r'[^가-힣a-zA-Z0-9\-]', '-', title.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')[:60]

    # 타입 결정
    page_type = "concept"
    if any(k in agent for k in ("dev", "research", "qa")):
        page_type = "how-to"
    elif any(k in agent for k in ("evaluator", "retro")):
        page_type = "reference"

    target_dir = wiki_dir / ("concepts" if page_type in ("concept", "how-to") else "entities")
    target_dir.mkdir(parents=True, exist_ok=True)
    wiki_path = target_dir / f"{slug}.md"

    # 기존 파일과 내용 비교 (의미적 중복 방지)
    if wiki_path.exists():
        existing = wiki_path.read_text(encoding="utf-8", errors="ignore")
        existing_kw = extract_keywords(existing)
        new_kw = extract_keywords(page["text_preview"])
        sim = keyword_similarity(existing_kw, new_kw)
        if sim > 0.85:
            _log(f"wiki 스킵 (의미 중복 {sim:.2f}): {slug}")
            return wiki_path  # 이미 동일 내용 존재
        # 부분 업데이트: 새 정보만 append
        # BUG-WIKI-YAML-3 수정 (2026-07-30): 기존 파일에 이중삽입 있으면 write 전 정리
        import re as _re
        _fm = _re.match(r"^(---\n.*?\n---\n)", existing, _re.DOTALL)
        if _fm:
            _body = existing[_fm.end():]
            _cleaned = _re.sub(r"\n---\n(?=[a-z_\'\"]).+?(?=\n##|\n# |\Z)", "\n", _body, flags=_re.DOTALL)
            existing = existing[:_fm.end()] + _cleaned
        update_section = (
            f"\n\n## 업데이트 ({today})\n"
            f"*KB 소스: {page['rel']} (agent: {agent})*\n\n"
            f"{page['text_preview'][:400]}\n"
        )
        wiki_path.write_text(existing + update_section, encoding="utf-8")
        _log(f"wiki 업데이트 (부분): {slug}")
        return wiki_path

    # 신규 wiki 페이지 생성
    content = f"""---
title: {title}
created: {today}
updated: {today}
type: {page_type}
agent: {agent}
tags: [{", ".join(keywords[:5])}]
kb_source: {page['rel']}
---

# {title}

*KB 동기화: {today} | 에이전트: {agent}*

{page['text_preview'][:800]}

## 관련 문서

<!-- 연결 페이지는 wiki crosslink 엔진이 자동 추가 -->
"""
    wiki_path.write_text(content, encoding="utf-8")
    _log(f"wiki 신규 생성: {slug}")
    return wiki_path


# ── wiki index.md 자동 갱신 ───────────────────────────────────────────────────
def update_wiki_index(wiki_dir: Path = WIKI_DIR) -> None:
    """wiki/index.md 갱신 (concept/entity 분류)"""
    index_path = wiki_dir / "index.md"
    today = datetime.now().strftime("%Y-%m-%d")

    concepts  = sorted((wiki_dir / "concepts").glob("*.md")) if (wiki_dir / "concepts").exists() else []
    entities  = sorted((wiki_dir / "entities").glob("*.md")) if (wiki_dir / "entities").exists() else []

    def _page_title(p: Path) -> str:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'^title:\s*(.+)$', text, re.MULTILINE)
            return m.group(1).strip() if m else p.stem
        except Exception:
            return p.stem

    def _page_desc(p: Path) -> str:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            # frontmatter 이후 첫 비어있지 않은 줄
            lines = text.split("\n")
            in_fm = False
            fm_done = False
            for line in lines:
                if line.strip() == "---":
                    if not in_fm:
                        in_fm = True
                    else:
                        fm_done = True
                    continue
                if fm_done and line.strip() and not line.startswith("#"):
                    return line.strip()[:80]
            return ""
        except Exception:
            return ""

    lines = [
        "# Wiki Index",
        "",
        f"> NOVA + HMG 지식베이스. Last updated: {today} | Total pages: {len(concepts)+len(entities)}",
        "",
        "## Concepts (기술/방법론)",
        "",
    ]
    for p in concepts:
        title = _page_title(p)
        desc = _page_desc(p)
        link = f"[[{p.stem}]]"
        lines.append(f"- {link} — {desc}" if desc else f"- {link} — {title}")

    lines += ["", "## Entities (시스템/인프라)", ""]
    for p in entities:
        title = _page_title(p)
        desc = _page_desc(p)
        link = f"[[{p.stem}]]"
        lines.append(f"- {link} — {desc}" if desc else f"- {link} — {title}")

    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(f"wiki index 갱신: concepts={len(concepts)}, entities={len(entities)}")


# ── memory 아카이브 → KB + wiki 정확 기록 ────────────────────────────────────
def archive_memory_section_to_kb(section_text: str, section_title: str, agent: str = "memory") -> Path | None:
    """
    memory_slim 아카이브 → KB에 정확히 기록 → wiki 연결.
    의미적 중복이 있으면 기존 페이지에 병합만 (새 파일 생성 안 함).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r'[^가-힣a-zA-Z0-9\-]', '-', section_title.lower())[:50].strip('-')
    if not slug:
        slug = f"memory-archive-{today}"

    kb_archive = KB_ROOT / "memory_archive"
    kb_archive.mkdir(parents=True, exist_ok=True)
    kb_path = kb_archive / f"mem-{today}-{slug}.md"

    # 기존 아카이브와 중복 체크
    new_kw = extract_keywords(section_text)
    for existing_md in kb_archive.glob(f"mem-{today}-*.md"):
        try:
            existing_text = existing_md.read_text(encoding="utf-8", errors="ignore")
            existing_kw = extract_keywords(existing_text)
            sim = keyword_similarity(existing_kw, new_kw)
            if sim > 0.80:
                _log(f"KB 아카이브 스킵 (의미 중복 {sim:.2f}): {existing_md.name}")
                return existing_md
        except Exception:
            pass

    # KB 기록
    kb_content = f"""---
title: "[memory-archive] {section_title}"
created: {today}
updated: {today}
type: reference
agent: {agent}
archived_from: MEMORY.md
tags: [{", ".join(new_kw[:5])}]
---

# {section_title}

*메모리 아카이브: {today} | 원본: Hermes MEMORY.md*

{section_text}
"""
    kb_path.write_text(kb_content, encoding="utf-8")
    _log(f"KB 아카이브 기록: {kb_path.name}")

    # wiki 연결
    wiki_page = kb_page_to_wiki({
        "path": kb_path,
        "rel": str(kb_path.relative_to(KB_ROOT)),
        "title": section_title,
        "agent": agent,
        "keywords": new_kw,
        "content_hash": hashlib.md5(kb_content.encode()).hexdigest(),
        # BUG-WIKI-YAML-2 수정: section_text에도 frontmatter 제거 적용
        "text_preview": re.sub(r'^---.*?---\\s*', '', section_text, count=1, flags=re.DOTALL)[:500],
    })
    if wiki_page:
        update_wiki_index()
    return kb_path


# ── 메인 동기화 ───────────────────────────────────────────────────────────────
def sync_kb_to_wiki(kb_root: Path = KB_ROOT, wiki_dir: Path = WIKI_DIR) -> dict:
    """KB 전체 → wiki 동기화"""
    _log("KB → wiki 동기화 시작")
    pages = load_kb_pages(kb_root)
    _log(f"KB 페이지: {len(pages)}개")

    # 중복 감지
    dups = find_duplicates(pages, threshold=0.55)
    if dups:
        _log(f"의미적 중복 감지: {len(dups)}쌍")
        for sim, p1, p2 in dups[:5]:
            _log(f"  중복({sim:.2f}): {p1['title'][:40]} ↔ {p2['title'][:40]}")

    # wiki 동기화 (신규 또는 갱신이 필요한 페이지만)
    synced = 0
    for page in pages:
        # wiki에 이미 있는지 확인 (slug 기반)
        slug = re.sub(r'[^가-힣a-zA-Z0-9\-]', '-', page["title"].lower())[:60].strip('-')
        existing_wiki = list(WIKI_DIR.rglob(f"{slug}.md"))
        if not existing_wiki:
            wiki_path = kb_page_to_wiki(page, wiki_dir)
            if wiki_path:
                synced += 1

    update_wiki_index(wiki_dir)
    _log(f"wiki 동기화 완료: {synced}개 신규/갱신")
    return {"total_kb": len(pages), "synced_wiki": synced, "duplicates": len(dups)}


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KB + wiki 통합 브릿지")
    parser.add_argument("--sync",        action="store_true", help="KB → wiki 동기화")
    parser.add_argument("--check-dup",   action="store_true", help="의미적 중복 감지")
    parser.add_argument("--archive",     type=str,            help="메모리 섹션 → KB+wiki")
    parser.add_argument("--title",       type=str,            default="", help="아카이브 제목")
    args = parser.parse_args()

    if args.sync:
        result = sync_kb_to_wiki()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.check_dup:
        pages = load_kb_pages()
        dups = find_duplicates(pages, threshold=0.55)
        print(f"의미적 중복 {len(dups)}쌍 발견:")
        for sim, p1, p2 in dups[:10]:
            print(f"  [{sim:.2f}] {p1['title'][:40]} ↔ {p2['title'][:40]}")

    elif args.archive:
        text = Path(args.archive).read_text(encoding="utf-8") if Path(args.archive).exists() else args.archive
        title = args.title or "memory-archive"
        result = archive_memory_section_to_kb(text, title)
        print(f"아카이브 완료: {result}")
