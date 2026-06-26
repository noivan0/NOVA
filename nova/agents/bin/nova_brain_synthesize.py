#!/usr/bin/env python3
"""
nova_brain_synthesize.py — 대화 세션 → KB 자동 변환 (GBrain synthesize 패턴)

세션에서 중요한 발견/결정/사실을 자동으로 KB 페이지로 변환.
kb_watcher가 30초 내 감지 → kb_pipeline Layer 4 → nova_brain.db 인덱싱.

사용:
  python3 nova_brain_synthesize.py --session <session_id>   # 특정 세션 처리
  python3 nova_brain_synthesize.py --recent <N>             # 최근 N개 세션
  python3 nova_brain_synthesize.py --auto                   # cron 패턴 (매일 실행)
"""
import sys
import os
import json
import re
import hashlib
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

KB_ROOT     = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
AGENTS_KB   = KB_ROOT / "agents"
LESSONS_KB  = KB_ROOT / "lessons"  # 실수/오판 lesson_learned 전용
SESSIONS_DB = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db"  # hermes는 state.db 사용 (sessions.db 없음)

# LLM API — nova_llm.py 공용 헬퍼 사용 (HMG haiku 미지원 → sonnet-4-6)
import sys as _sys
_sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_llm import call_llm  # noqa: E402


def get_recent_sessions(n: int = 5, skip_cron: bool = True) -> list:
    """최근 세션 목록 조회 (기본값: cron 세션 제외)"""
    import sqlite3
    if not SESSIONS_DB.exists():
        return []
    with sqlite3.connect(str(SESSIONS_DB)) as conn:
        if skip_cron:
            rows = conn.execute("""
                SELECT id, title, started_at FROM sessions
                WHERE source != 'cron'
                ORDER BY started_at DESC LIMIT ?
            """, (n,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, title, started_at FROM sessions
                ORDER BY started_at DESC LIMIT ?
            """, (n,)).fetchall()
    return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]


def get_session_text(session_id: str) -> str:
    """세션 메시지를 텍스트로 추출"""
    import sqlite3
    if not SESSIONS_DB.exists():
        return ""
    with sqlite3.connect(str(SESSIONS_DB)) as conn:
        rows = conn.execute("""
            SELECT role, content FROM messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY timestamp
        """, (session_id,)).fetchall()
    parts = []
    for role, content in rows:
        if isinstance(content, str):
            parts.append(f"[{role}]: {content[:500]}")
    return "\n".join(parts)[:8000]


def should_synthesize(session_text: str) -> bool:
    """Haiku로 처리 가치 판단 (GBrain synthesize 1단계)"""
    prompt = f"""다음 대화에 KB에 저장할 만한 중요 정보가 있나요?
중요 정보 = 기술적 발견, 버그 수정, 설계 결정, 프로젝트 상태, 중요 수치

대화:
{session_text[:2000]}

답변: YES 또는 NO 만 (이유 없이)"""
    result = call_llm(prompt, max_tokens=10)
    return "YES" in result.upper()


def should_learn_from_error(session_text: str) -> bool:
    """실수/오판/실패 접근법 감지 — lesson_learned KB 생성 트리거"""
    prompt = f"""다음 대화에 실수, 오판, 잘못된 접근법이 있나요?
감지 대상 = 버그 원인 오해, 잘못된 명령 실행, 설계 실수, 잘못된 판단 후 수정,
재발 방지가 필요한 패턴, false alarm 오탐, 롤백/재시도 상황

대화:
{session_text[:2000]}

답변: YES 또는 NO 만 (이유 없이)"""
    result = call_llm(prompt, max_tokens=10)
    return "YES" in result.upper()


def extract_lesson(session_text: str, session_title: str) -> dict:
    """실수/오판 세션에서 lesson_learned 추출"""
    prompt = f"""다음 대화에서 발생한 실수/오판/잘못된 접근법을 분석하세요.

세션: {session_title}

대화:
{session_text}

다음 JSON 형식으로만 답변:
{{
  "title": "실수 제목 (짧게)",
  "situation": "어떤 맥락에서 발생했는가 (1-2줄)",
  "what_went_wrong": "무엇을 잘못했는가 (구체적으로)",
  "root_cause": "왜 잘못 판단했는가 — 재발 방지 핵심",
  "fix": "어떻게 해결했는가",
  "prevention": "다음에 같은 상황에서 → 이렇게 행동",
  "tags": ["태그1", "태그2"],
  "agent": "nova-research|nova-dev|nova-qa|nova-evaluator|nova-strategy|hermes"
}}"""
    result = call_llm(prompt, max_tokens=800)
    try:
        m = re.search(r'\{.*\}', result, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {}


def save_lesson_to_kb(lesson: dict, session_id: str) -> Path:
    """실수/오판 lesson_learned를 kb/lessons/ 에 저장"""
    today = datetime.now().strftime("%Y-%m-%d")
    sid_short = session_id[:8] if session_id else "unknown"
    title = lesson.get("title", "lesson")
    safe_title = re.sub(r'[^\w\-]', '-', title)[:40].lower()

    LESSONS_KB.mkdir(parents=True, exist_ok=True)
    path = LESSONS_KB / f"lesson-{today}-{sid_short}-{safe_title}.md"

    content = f"""---
title: {title}
page_type: lesson_learned
agent: {lesson.get('agent', 'hermes')}
source: synthesized
session_id: {session_id}
date: {today}
tags: {', '.join(lesson.get('tags', []) + ['lesson'])}
synthesized_at: {datetime.now(timezone.utc).isoformat()}
---

# {title} — {today}

## 상황
{lesson.get('situation', '')}

## 실수/오판 내용
{lesson.get('what_went_wrong', '')}

## 근거 착오
{lesson.get('root_cause', '')}

## 수정 방법
{lesson.get('fix', '')}

## 재발 방지 원칙
{lesson.get('prevention', '')}

## Timeline

> 이 섹션은 추가전용입니다.

- {today}: [lesson] 세션에서 자동 추출됨 (session: {session_id[:8]})
"""
    path.write_text(content, encoding="utf-8")
    return path


def extract_knowledge(session_text: str, session_title: str) -> dict:
    """Sonnet으로 지식 추출 (GBrain synthesize 2단계)"""
    prompt = f"""다음 대화에서 KB 페이지로 저장할 핵심 정보를 추출하세요.

세션: {session_title}

대화:
{session_text}

다음 JSON 형식으로만 답변:
{{
  "title": "짧은 제목",
  "page_type": "project|concept|fix|decision|finding",
  "compiled_truth": "현재 최선의 이해 (마크다운, 200-500자)",
  "timeline_entry": "오늘 날짜의 타임라인 항목 (1-2줄)",
  "tags": ["태그1", "태그2"],
  "agent": "nova-research|nova-dev|nova-qa|nova-evaluator|nova-strategy|hermes"
}}"""
    result = call_llm(prompt, max_tokens=800)
    try:
        # JSON 추출
        m = re.search(r'\{.*\}', result, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {}


def save_to_kb(knowledge: dict, session_id: str) -> Path:
    """추출된 지식을 KB 파일로 저장 (CT+TL 구조)"""
    today = datetime.now().strftime("%Y-%m-%d")
    sid_short = session_id[:8] if session_id else "unknown"
    title = knowledge.get("title", "synthesized")
    safe_title = re.sub(r'[^\w\-]', '-', title)[:40].lower()

    # 저장 경로: agent 별로 분리
    agent = knowledge.get("agent", "hermes")
    if agent in ("nova-research","nova-dev","nova-qa","nova-evaluator","nova-strategy"):
        save_dir = AGENTS_KB / agent
    else:
        save_dir = KB_ROOT / "projects"  # 기본

    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"synth-{today}-{sid_short}-{safe_title}.md"

    content = f"""---
title: {knowledge.get('title', title)}
agent: {agent}
page_type: {knowledge.get('page_type', 'general')}
source: synthesized
session_id: {session_id}
date: {today}
tags: {', '.join(knowledge.get('tags', []))}
synthesized_at: {datetime.now(timezone.utc).isoformat()}
---

## Compiled Truth

{knowledge.get('compiled_truth', '')}

## Timeline

> 이 섹션은 추가전용입니다.

- {today}: [synthesize] {knowledge.get('timeline_entry', '세션에서 자동 추출됨')} (session: {session_id[:8]})
"""
    path.write_text(content, encoding="utf-8")
    return path


def process_session(session_id: str, session_title: str = "") -> bool:
    """단일 세션 처리"""
    text = get_session_text(session_id)
    if not text or len(text) < 200:
        print(f"  SKIP: {session_id[:8]} (내용 부족)")
        return False

    saved = False

    # 1a단계: 일반 지식 판단 + 추출
    if should_synthesize(text):
        knowledge = extract_knowledge(text, session_title)
        if knowledge and knowledge.get("compiled_truth"):
            path = save_to_kb(knowledge, session_id)
            print(f"  SAVED: {path.relative_to(KB_ROOT)} — {knowledge.get('title')}")
            saved = True
        else:
            print(f"  SKIP: {session_id[:8]} (추출 실패 — knowledge)")
    else:
        print(f"  SKIP: {session_id[:8]} (일반 지식 없음)")

    # 1b단계: 실수/오판 감지 + lesson_learned 추출 (독립 실행)
    if should_learn_from_error(text):
        lesson = extract_lesson(text, session_title)
        if lesson and lesson.get("what_went_wrong"):
            path = save_lesson_to_kb(lesson, session_id)
            print(f"  LESSON: {path.relative_to(KB_ROOT)} — {lesson.get('title')}")
            saved = True
        else:
            print(f"  SKIP: {session_id[:8]} (추출 실패 — lesson)")

    return saved


def run_auto(days_back: int = 1):
    """자동 실행: 최근 N일 세션 중 미처리 세션 synthesize"""
    import sqlite3
    if not SESSIONS_DB.exists():
        print("state.db 없음")
        return

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp()
    with sqlite3.connect(str(SESSIONS_DB)) as conn:
        rows = conn.execute("""
            SELECT id, title, started_at FROM sessions
            WHERE started_at > ? AND source != 'cron'
            ORDER BY started_at DESC
        """, (cutoff_ts,)).fetchall()

    # 이미 synthesize된 세션 확인
    synthesized = set()
    for f in (AGENTS_KB).rglob("synth-*.md"):
        m = re.search(r'synth-[\d-]+-([a-f0-9]{8})-', f.name)
        if m:
            synthesized.add(m.group(1))

    print(f"총 {len(rows)}개 세션, {len(synthesized)}개 이미 처리됨")
    processed = 0
    for session_id, title, created_at in rows:
        sid_short = session_id[:8]
        if sid_short in synthesized:
            continue
        print(f"처리: {sid_short} — {title or '(무제)'}")
        ok = process_session(session_id, title or "")
        if ok:
            processed += 1

    print(f"\nsynthesize 완료: {processed}개 새 KB 항목")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()

    parser = argparse.ArgumentParser()
    parser.add_argument("--session", help="특정 세션 ID 처리")
    parser.add_argument("--recent", type=int, help="최근 N개 세션 처리")
    parser.add_argument("--auto", action="store_true", help="자동 모드 (최근 1일)")
    parser.add_argument("--days", type=int, default=1, help="--auto 기준 일수")
    args = parser.parse_args()

    if args.session:
        ok = process_session(args.session)
        print("처리됨" if ok else "스킵")
    elif args.recent:
        sessions = get_recent_sessions(args.recent)
        for s in sessions:
            print(f"처리: {s['id'][:8]} — {s.get('title','')}")
            process_session(s["id"], s.get("title",""))
    elif args.auto:
        run_auto(days_back=args.days)
    else:
        parser.print_help()
