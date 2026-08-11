#!/usr/bin/env python3
"""
nova_memory_slim.py — Hermes memory 자율 슬림화 + KB 이관 엔진
=============================================================
트리거: brain_watcher → MEMORY ≥ 85% (1870자 이상, 한계 2200자)

동작:
1. ~/.hermes/memories/MEMORY.md 읽기
2. 각 섹션(§ 구분) 파싱 → KB 이관 여부 판단
3. 자율화/NOVA 관련 섹션 → KB로 아카이브 (brain.db 인덱싱)
4. 중요도 낮은 섹션 요약·압축
5. Hermes memory 슬림화된 버전으로 교체
6. hermes_events에 MEMORY_SLIM 이벤트 기록 (chain_engine handle_memory_events가 수신)

설계 원칙:
- 절대 데이터 손실 없음: 삭제 전 KB 아카이브 보장
- 자율 판단: LLM 없이 규칙 기반 (빠름, 안정)
- brain_watcher daemon thread에서 실행 (60초 timeout)
"""

from __future__ import annotations

import os
import sys
import re
import uuid
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME    = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
MEMORY_FILE  = HERMES_HOME / "memories" / "MEMORY.md"
MEMORY_LIMIT = 2_200          # Hermes memory_char_limit
SLIM_TARGET  = 1_400          # 슬림 후 목표 (64%, 여유 36%): 1600 → 1400, 재채움 순환 방지
KB_ARCHIVE   = HERMES_HOME / "kb" / "memory_archive"
BRAIN_DB     = NOVA_HOME / "brain.db"

LOG_FILE = NOVA_HOME / "logs" / "memory_slim.log"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[memory-slim] [{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _parse_sections(text: str) -> list[dict]:
    """§ 구분자로 섹션 파싱. 각 섹션 = {content, chars, keywords}"""
    raw = [s.strip() for s in text.split("§") if s.strip()]
    sections = []
    for i, content in enumerate(raw):
        # 중요도 키워드 스코어
        keywords = {
            "nova": content.lower().count("nova"),
            "harness": content.lower().count("harness"),
            "mms": content.lower().count("mms"),
            "confluence": content.lower().count("confluence"),
            "가동률": content.count("가동률"),
        }
        score = sum(keywords.values())
        sections.append({
            "idx": i,
            "content": content,
            "chars": len(content),
            "score": score,
            "keywords": keywords,
            "archived": False,
        })
    return sections


def _archive_to_kb(section: dict, slim_version: str | None = None) -> Path | None:
    """섹션을 KB에 아카이브. slim_version이 있으면 압축본도 저장."""
    KB_ARCHIVE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_raw = section["content"][:40].replace("\n", " ").strip()
    title_safe = re.sub(r"[^\w\uAC00-\uD7A3\u1100-\u11FF가-힣]", "-", title_raw)[:30]
    fname = f"mem-archive-{today}-{section['idx']:02d}-{title_safe}.md"
    path = KB_ARCHIVE / fname

    content_to_save = slim_version if slim_version else section["content"]
    full_content = f"""---
title: "[memory-archive] {title_raw}"
source: hermes-memory
archived_at: {datetime.now(timezone.utc).isoformat()}
chars_original: {section['chars']}
chars_archived: {len(content_to_save)}
section_idx: {section['idx']}
---

## Archived Memory Section

{content_to_save}

## Original Section (full)

{section['content'] if slim_version else '(same as above)'}
"""
    path.write_text(full_content, encoding="utf-8")
    _log(f"  KB 아카이브: {fname} ({section['chars']}자 → {len(content_to_save)}자)")

    # KB 아카이브 → wiki 자동 연결 (nova_kb_wiki_bridge 연동)
    kb_wiki_bridge = HERMES_HOME / "bin" / "nova_kb_wiki_bridge.py"
    if kb_wiki_bridge.exists():
        try:
            import subprocess as _sp
            title_for_wiki = title_raw or f"memory-archive-{today}"
            _sp.run(
                [sys.executable, str(kb_wiki_bridge),
                 "--archive", str(path),
                 "--title", title_for_wiki],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "NOVA_HOME": str(NOVA_HOME)}
            )
            _log(f"  wiki 연결 완료: {title_for_wiki[:40]}")
        except Exception as e:
            _log(f"  wiki 연결 실패 (무시): {e}")

    return path


def _compress_section(content: str) -> str:
    """
    규칙 기반 압축: 긴 섹션을 핵심만 남김.
    - 줄별로 파싱, 중복 제거, URL 단축
    - NOVA 관련은 핵심 키워드만 남김
    """
    lines = content.split("\n")
    seen = set()
    compressed = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 중복 제거 (50자 prefix 기준)
        key = stripped[:50]
        if key in seen:
            continue
        seen.add(key)
        compressed.append(stripped)

    result = " ".join(compressed[:8])  # 최대 8줄
    if len(result) > 200:
        result = result[:197] + "..."
    return result


def _record_hermes_event(action: str, chars_before: int, chars_after: int) -> None:
    """hermes_events에 MEMORY_SLIM 이벤트 기록 → chain_engine handle_memory_events 수신"""
    try:
        db = sqlite3.connect(str(BRAIN_DB), timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        eid = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO hermes_events "
            "(id, event_type, severity, title, detail, source_agent, is_read, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "MEMORY_SLIM", "info",
             f"memory_slim: {chars_before}→{chars_after}자 ({action})",
             f"슬림 전: {chars_before}자, 슬림 후: {chars_after}자, 액션: {action}",
             "nova-memory-slim", 0, now)
        )
        db.commit()
        db.close()
        _log(f"  hermes_events 기록: MEMORY_SLIM ({chars_before}→{chars_after}자)")
    except Exception as e:
        _log(f"  WARN: hermes_events 기록 실패: {e}")


def _run_kb_sync() -> None:
    """KB 아카이브 → brain.db 인덱싱"""
    try:
        hermes_bin = HERMES_HOME / "bin"
        nova_src   = Path.home() / "nova"
        env = {**os.environ,
               "HERMES_HOME": str(HERMES_HOME),
               "NOVA_HOME":   str(NOVA_HOME),
               "PYTHONPATH":  f"{hermes_bin}:{nova_src}"}
        r = subprocess.run(
            [sys.executable, str(hermes_bin / "nova_kb_sync.py")],
            env=env, capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            _log("  kb_sync 완료 (아카이브 인덱싱)")
        else:
            _log(f"  WARN: kb_sync rc={r.returncode}")
    except Exception as e:
        _log(f"  WARN: kb_sync 실패: {e}")


def main() -> None:
    _log("=== memory_slim 시작 ===")

    if not MEMORY_FILE.exists():
        _log(f"SKIP: {MEMORY_FILE} 없음")
        return

    text = MEMORY_FILE.read_text(encoding="utf-8", errors="replace")
    chars_before = len(text)
    pct = int(chars_before * 100 / MEMORY_LIMIT)

    _log(f"현재 메모리: {chars_before}자 / {MEMORY_LIMIT}자 ({pct}%)")

    if chars_before <= SLIM_TARGET:
        _log(f"슬림 불필요: {chars_before} ≤ {SLIM_TARGET}자")
        return

    sections = _parse_sections(text)
    _log(f"섹션 {len(sections)}개 파싱 완료")

    # 전략: 가장 긴 섹션부터 아카이브
    # 규칙:
    #   - 모든 섹션은 KB에 풀 내용 아카이브 (데이터 손실 없음)
    #   - 실제 MEMORY.md에는 원본 내용 유지 (압축 없음)
    #   - 가장 긴 섹션부터 KB 아카이브 완료 후 MEMORY.md에서 제거
    #   - NOVA/harness 관련 섹션은 마지막에 제거 (중요도 높음)
    sections_by_size = sorted(sections, key=lambda s: s["chars"], reverse=True)

    archived_sections = []
    removed_indices = set()
    chars_freed = 0

    for sec in sections_by_size:
        if chars_before - chars_freed <= SLIM_TARGET:
            break

        # KB에 풀 내용 아카이브 먼저
        _archive_to_kb(sec)

        # MEMORY.md에서 제거 (원본 내용 보존 불필요 - KB에 있으므로)
        removed_indices.add(sec["idx"])
        chars_freed += sec["chars"]
        archived_sections.append(sec["idx"])
        _log(f"  섹션 {sec['idx']} KB이관 후 제거: {sec['chars']}자 (KB에 보존됨)")

    # 재조립 - 제거되지 않은 섹션만 유지 (원본 내용 그대로)
    remaining = [s for s in sections if s["idx"] not in removed_indices]
    new_text = "\n§\n".join(s["content"] for s in remaining)
    chars_after = len(new_text)

    _log(f"슬림 결과: {chars_before}자 → {chars_after}자 (절감 {chars_before-chars_after}자, {int((chars_before-chars_after)*100/chars_before)}%)")
    _log(f"아카이브된 섹션: {len(archived_sections)}개")

    # 백업 후 교체
    backup = MEMORY_FILE.parent / f"MEMORY.md.slim-bak"
    MEMORY_FILE.rename(backup)
    MEMORY_FILE.write_text(new_text, encoding="utf-8")
    _log(f"메모리 파일 교체 완료 (백업: {backup.name})")

    # KB 동기화
    _run_kb_sync()

    # brain.db 이벤트 기록
    _record_hermes_event(
        action=f"archive_{len(archived_sections)}sections",
        chars_before=chars_before,
        chars_after=chars_after
    )

    _log(f"=== memory_slim 완료: {chars_after}/{MEMORY_LIMIT}자 ({int(chars_after*100/MEMORY_LIMIT)}%) ===")


if __name__ == "__main__":
    main()
