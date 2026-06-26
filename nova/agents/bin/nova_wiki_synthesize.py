#!/usr/bin/env python3
"""
nova_wiki_synthesize.py — 계층7: llm-wiki 자율 갱신 (NOVA 자율성장)

NOVA 에이전트가 KB를 기반으로 wiki를 스스로 갱신:
  1. lesson_learned KB → wiki/concepts/ 자동 반영
  2. stale wiki 페이지 (90일+ 미갱신 + KB 원본 변경) → LLM으로 재생성
  3. 크로스링크 자동 삽입 — 동일 개념 다른 페이지 언급 시 [[wikilink]] 추가
  4. nova_brain.db takes 요약 → wiki entity 페이지 갱신
  5. wiki/index.md 자동 정리 (dead link 제거, 신규 항목 추가)

사용:
  python3 nova_wiki_synthesize.py           # 전체 실행
  python3 nova_wiki_synthesize.py --phase lessons    # 계층7a만
  python3 nova_wiki_synthesize.py --phase stale      # 계층7b만
  python3 nova_wiki_synthesize.py --phase crosslink  # 계층7c만
  python3 nova_wiki_synthesize.py --phase index      # 계층7e만
  python3 nova_wiki_synthesize.py --dry-run
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import os
import re
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))
from nova_llm import call_llm  # noqa: E402

# ── 경로 상수 ─────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
KB_ROOT     = HERMES_HOME / "kb"
KB_LESSONS  = KB_ROOT / "lessons"
WIKI_ROOT   = HERMES_HOME / "wiki"
NOVA_DB     = HERMES_HOME / "nova_brain.db"

WIKI_CONCEPTS = WIKI_ROOT / "concepts"
WIKI_ENTITIES = WIKI_ROOT / "entities"
WIKI_INDEX    = WIKI_ROOT / "index.md"
WIKI_LOG      = WIKI_ROOT / "log.md"

STALE_DAYS    = 90   # 이 이상 미갱신 + KB 변경 시 재생성


def _upsert_wiki_to_pages(wiki_path: Path, agent: str = "nova-document"):
    """생성된 wiki 파일을 nova_brain.db pages에 즉시 동기화"""
    import uuid
    db = sqlite3.connect(str(NOVA_DB), timeout=3)
    try:
        c = db.cursor()
        content_text = wiki_path.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(wiki_path.relative_to(Path.home() / ".hermes"))
        existing = c.execute("SELECT id FROM pages WHERE path=?", (rel_path,)).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        char_count = len(content_text)
        if existing:
            c.execute(
                "UPDATE pages SET compiled_truth=?, timeline=?, char_count=?, updated_at=?, agent=?, page_type=? WHERE path=?",
                (content_text[:8000], "", char_count, now, agent, 'wiki', rel_path)
            )
        else:
            pid = uuid.uuid4().hex[:16]
            title = wiki_path.stem.replace('-', ' ').replace('_', ' ')[:120]
            c.execute(
                "INSERT INTO pages (id, path, title, agent, page_type, compiled_truth, timeline, char_count, indexed_at, updated_at, health_score, has_contradictions, emotional_weight) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, rel_path, title, agent, 'wiki', content_text[:8000], "", char_count, now, now, 1.0, 0, 0.0)
            )
        db.commit()
    except Exception as e:
        print(f"  [WARN] _upsert_wiki_to_pages 실패 ({wiki_path.name}): {e}")
    finally:
        db.close()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def log_wiki(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{ts[:10]}] {msg}\n"
    try:
        with open(WIKI_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def _parse_frontmatter(content: str) -> dict:
    m = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            fm[k.strip()] = v.strip().strip('"\'')
    return fm


def _wiki_slug(name: str) -> str:
    return re.sub(r'[^\w\-]', '-', name).lower()[:60]


# ── Phase 7a: lesson_learned → wiki ───────────────────

def phase_lessons(dry_run: bool = False) -> int:
    """kb/lessons/ 신규 파일 → wiki/concepts/ 자동 생성"""
    if not KB_LESSONS.exists():
        return 0
    lesson_files = list(KB_LESSONS.glob("lesson-*.md"))
    added = 0
    for lf in lesson_files:
        content = lf.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        title = fm.get("title", lf.stem)
        slug = _wiki_slug(lf.stem)
        wiki_path = WIKI_CONCEPTS / f"{slug}.md"

        if wiki_path.exists():
            continue  # 이미 반영됨

        # LLM으로 wiki 요약 생성
        prompt = f"""다음 lesson_learned KB를 wiki 개념 페이지로 변환하세요.

{content[:3000]}

다음 형식으로 변환:
---
title: (제목)
created: {today()}
updated: {today()}
type: concept
tags: [lesson, prevention, {fm.get('agent', 'hermes')}]
sources: [{lf.relative_to(KB_ROOT)}]
---

# (제목)

> lesson_learned 자동 반영. 원본: `kb/{lf.relative_to(KB_ROOT)}`

## 핵심 교훈

(what_went_wrong + root_cause를 1-2줄로)

## 재발 방지

(prevention 내용)

## 관련 항목

- (관련 개념 [[wikilink]] 2개 이상)
"""
        result = call_llm(prompt, max_tokens=600)
        if not result or len(result) < 100:
            continue

        if not dry_run:
            WIKI_CONCEPTS.mkdir(parents=True, exist_ok=True)
            wiki_path.write_text(result, encoding="utf-8")
            log_wiki(f"lesson→wiki | {lf.name} → concepts/{slug}.md")
            print(f"  [LESSON] {lf.name} → wiki/concepts/{slug}.md")
            _upsert_wiki_to_pages(wiki_path)
        else:
            print(f"  [DRY] LESSON: {lf.name} → wiki/concepts/{slug}.md")
        added += 1

    return added


# ── Phase 7b: stale 페이지 재생성 ────────────────────

def phase_stale(dry_run: bool = False) -> int:
    """90일+ 미갱신 wiki 페이지 중 KB 원본이 변경된 것 재생성"""
    cutoff = datetime.now() - timedelta(days=STALE_DAYS)
    refreshed = 0
    wiki_files = list(WIKI_CONCEPTS.rglob("*.md")) + list(WIKI_ENTITIES.rglob("*.md"))

    for wf in wiki_files:
        try:
            content = wf.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            updated_str = fm.get("updated", fm.get("created", ""))
            if not updated_str:
                continue
            updated_dt = datetime.strptime(updated_str[:10], "%Y-%m-%d")
            if updated_dt > cutoff:
                continue  # 최근 갱신됨

            # KB 원본 존재 확인
            kb_path_str = fm.get("kb_path", "").strip("[[]]").replace("kb/", "", 1)
            if not kb_path_str:
                continue
            kb_path = KB_ROOT / kb_path_str
            if not kb_path.exists():
                continue

            # KB 원본이 wiki보다 더 최근에 수정됐는지 확인
            kb_mtime = datetime.fromtimestamp(kb_path.stat().st_mtime)
            if kb_mtime <= updated_dt:
                continue  # KB도 오래됨

            kb_content = kb_path.read_text(encoding="utf-8")
            prompt = f"""다음 KB 파일 내용을 기반으로 wiki 페이지를 최신화하세요.

KB 파일 ({kb_path_str}):
{kb_content[:3000]}

기존 wiki 페이지 (updated: {updated_str}):
{content[:1500]}

다음 wiki 페이지 형식으로 업데이트하세요 (frontmatter의 updated를 {today()}로 변경):
"""
            result = call_llm(prompt, max_tokens=600)
            if not result or len(result) < 100:
                continue

            if not dry_run:
                wf.write_text(result, encoding="utf-8")
                log_wiki(f"stale refresh | {wf.relative_to(WIKI_ROOT)} (갱신: {updated_str}→{today()})")
                print(f"  [STALE] 갱신: {wf.relative_to(WIKI_ROOT)}")
            else:
                print(f"  [DRY] STALE: {wf.relative_to(WIKI_ROOT)}")
            refreshed += 1

        except Exception as e:
            print(f"  [WARN] stale 처리 오류 {wf.name}: {e}")

    return refreshed


# ── Phase 7c: 크로스링크 자동 삽입 ───────────────────

def phase_crosslink(dry_run: bool = False) -> int:
    """wiki 페이지 간 동일 개념 언급 시 [[wikilink]] 자동 삽입"""
    wiki_files = list(WIKI_CONCEPTS.rglob("*.md")) + list(WIKI_ENTITIES.rglob("*.md"))

    # 모든 wiki 페이지 제목 인덱스 구축
    title_index = {}
    for wf in wiki_files:
        try:
            content = wf.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            title = fm.get("title", wf.stem)
            title_index[wf.stem] = title
        except Exception:
            pass

    linked = 0
    for wf in wiki_files:
        try:
            content = wf.read_text(encoding="utf-8")
            original = content
            stem = wf.stem

            for other_stem, other_title in title_index.items():
                if other_stem == stem:
                    continue
                # 이미 링크된 경우 스킵
                if f"[[{other_stem}]]" in content:
                    continue
                # 제목이 본문에 언급된 경우 링크 삽입
                if other_title and other_title in content and len(other_title) > 5:
                    content = content.replace(
                        other_title,
                        f"[[{other_stem}|{other_title}]]",
                        1  # 첫 번째 언급만
                    )

            if content != original:
                if not dry_run:
                    wf.write_text(content, encoding="utf-8")
                    log_wiki(f"crosslink | {wf.relative_to(WIKI_ROOT)}")
                linked += 1

        except Exception as e:
            print(f"  [WARN] crosslink 오류 {wf.name}: {e}")

    return linked


# ── Phase 7d: nova_brain.db takes → wiki entity 갱신 ──

def phase_nova_takes(dry_run: bool = False) -> int:
    """nova_brain.db 주요 takes → wiki/entities/ 요약 페이지 갱신"""
    if not NOVA_DB.exists():
        return 0
    conn = sqlite3.connect(str(NOVA_DB))
    # 스키마 자동 감지 (헤르: claim/created_at, 헤르2: content)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(takes)").fetchall()]
    claim_col = "claim" if "claim" in cols else "content"
    date_col  = "created_at" if "created_at" in cols else "recorded_at"

    # 스킬 관련이 아닌 주요 takes (최근 30일)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        rows = conn.execute(f"""
            SELECT {claim_col}, {date_col} FROM takes
            WHERE {claim_col} NOT LIKE '%[skill:%'
            AND {date_col} > ?
            AND superseded_by IS NULL
            ORDER BY {date_col} DESC
            LIMIT 50
        """, (cutoff,)).fetchall()
    except Exception:
        # superseded_by 없는 스키마 폴백
        rows = conn.execute(f"""
            SELECT {claim_col}, {date_col} FROM takes
            WHERE {claim_col} NOT LIKE '%[skill:%'
            AND {date_col} > ?
            ORDER BY {date_col} DESC
            LIMIT 50
        """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        return 0

    # takes 요약 → wiki entity 갱신
    takes_text = "\n".join(f"- {r[0][:200]}" for r in rows[:30])
    wiki_path = WIKI_ENTITIES / "nova-brain-takes-summary.md"

    existing = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""

    prompt = f"""다음 NOVA brain takes(30일 이내 핵심 지식)를 wiki entity 요약 페이지로 작성하세요.

Takes 목록:
{takes_text}

기존 wiki 페이지:
{existing[:1000] if existing else '(없음)'}

다음 형식으로:
---
title: NOVA Brain Takes 요약
created: {today()}
updated: {today()}
type: entity
tags: [nova, takes, knowledge, summary]
sources: [nova_brain.db]
---

# NOVA Brain Takes 요약

> nova_brain.db 최근 30일 핵심 지식 자동 추출. 갱신: {today()}

## 핵심 발견

(상위 10개 takes를 카테고리별로 그룹화해서 마크다운 리스트로)

## 관련 항목

- [[nova-architecture]]
- [[nova-brain-takes-summary]]
"""
    result = call_llm(prompt, max_tokens=800)
    if not result or len(result) < 100:
        return 0

    if not dry_run:
        WIKI_ENTITIES.mkdir(parents=True, exist_ok=True)
        wiki_path.write_text(result, encoding="utf-8")
        log_wiki(f"nova-takes | {len(rows)}개 takes → entities/nova-brain-takes-summary.md")
        print(f"  [TAKES] {len(rows)}개 → wiki/entities/nova-brain-takes-summary.md")
        _upsert_wiki_to_pages(wiki_path, agent="nova-synthesize")
        # nova_brain.db에 high-weight takes 기록
        try:
            import sqlite3 as _sq, uuid as _uuid, datetime as _dt
            _DB = str(NOVA_DB)  # Codex MEDIUM fix: 상단 NOVA_DB 상수 사용 (Path.home() 하드코딩 제거)
            _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            _dba = _sq.connect(_DB)
            # wiki 페이지 page_id 조회
            _page = _dba.execute(
                "SELECT id FROM pages WHERE path LIKE '%nova-brain-takes-summary%' LIMIT 1"
            ).fetchone()
            if _page:
                _existing = _dba.execute(
                    "SELECT count(*) FROM takes WHERE page_id=? AND weight >= 0.85", (_page[0],)
                ).fetchone()[0]
                if not _existing:
                    _dba.execute(
                        "INSERT INTO takes (page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                        (_page[0], "fact", "nova-synthesize",
                         f"NOVA Brain Takes 요약 wiki entity — LLM 합성 {_now[:10]}", 0.88, _now, _now)
                    )
                    _dba.commit()
            _dba.close()
        except Exception:
            pass
    else:
        print(f"  [DRY] TAKES: {len(rows)}개 → wiki/entities/nova-brain-takes-summary.md")
    return 1


# ── Phase 7e: wiki/index.md 정리 ──────────────────────

def phase_index(dry_run: bool = False) -> int:
    """wiki/index.md dead link 제거 + 신규 항목 추가"""
    if not WIKI_INDEX.exists():
        return 0

    index_content = WIKI_INDEX.read_text(encoding="utf-8")
    all_wiki_stems = set()
    for wf in WIKI_ROOT.rglob("*.md"):
        if wf.name not in {"index.md", "log.md", "SCHEMA.md"}:
            all_wiki_stems.add(wf.stem)

    # dead link 감지 ([[foo]] 형태)
    # EXCLUDED_STEMS: 오탐 방지를 위해 아래 stem은 dead link 판정에서 제외
    #   "index"  — index.md 자기 자신 또는 서브디렉토리 index 참조
    #   "log"    — log.md (wiki 갱신 이력 파일) 참조
    #   "SCHEMA" — SCHEMA.md (DB 스키마 정의 파일) 참조
    # 새로운 시스템 파일 추가 시 이 집합에 stem을 추가해야 오탐이 발생하지 않음
    EXCLUDED_STEMS = {"index", "log", "SCHEMA"}
    linked = set(re.findall(r'\[\[([^\]|]+)\]\]', index_content))
    dead = linked - all_wiki_stems - EXCLUDED_STEMS
    if dead:
        print(f"  [INDEX] dead link {len(dead)}개: {list(dead)[:5]}")
        for d in dead:
            index_content = re.sub(r'\[\[' + re.escape(d) + r'[^\]]*\]\][^\n]*\n?', '', index_content)
        if not dry_run:
            WIKI_INDEX.write_text(index_content, encoding="utf-8")
            log_wiki(f"index cleanup | dead link {len(dead)}개 제거")
        return len(dead)
    return 0


# ── 메인 ──────────────────────────────────────────────

def run(phase: str = "all", dry_run: bool = False):
    results = {}
    print(f"[{today()}] nova_wiki_synthesize 시작" + (" [DRY-RUN]" if dry_run else ""))

    if phase in ("all", "lessons"):
        n = phase_lessons(dry_run)
        results["lessons"] = n
        if n: print(f"  lessons: {n}개 wiki 페이지 생성")

    if phase in ("all", "stale"):
        n = phase_stale(dry_run)
        results["stale"] = n
        if n: print(f"  stale: {n}개 페이지 갱신")

    if phase in ("all", "crosslink"):
        n = phase_crosslink(dry_run)
        results["crosslink"] = n
        if n: print(f"  crosslink: {n}개 페이지 링크 추가")

    if phase in ("all", "takes"):
        n = phase_nova_takes(dry_run)
        results["takes"] = n

    if phase in ("all", "index"):
        n = phase_index(dry_run)
        results["index"] = n
        if n: print(f"  index: {n}개 dead link 제거")

    total = sum(results.values())
    if total > 0:
        print(f"nova_wiki_synthesize 완료: {results}")
        # agent_activity 기록
        try:
            import datetime as _dt, sqlite3 as _sq
            _DB = f'{_HERMES_HOME}/nova_brain.db'
            _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            _dba = _sq.connect(_DB)
            _dba.execute(
                "INSERT INTO agent_activity (agent,action,summary,recorded_at) VALUES (?,?,?,?)",
                ('nova-synthesize', 'synthesize_cycle',
                 f"wiki_synthesize 완료: crosslink={results.get('crosslink',0)} lessons={results.get('lessons',0)}", _now)
            )
            _dba.commit()
            _dba.close()
        except Exception:
            pass
    # silent watchdog: 변경 없으면 출력 없음


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="all",
                        choices=["all", "lessons", "stale", "crosslink", "takes", "index"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(phase=args.phase, dry_run=args.dry_run)
