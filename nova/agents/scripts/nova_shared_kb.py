#!/usr/bin/env python3
"""
nova_shared_kb.py — 멀티에이전트 공유 지식베이스 (단일 진입점)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

모든 에이전트가 이 모듈을 통해 동일한 지식베이스에 접근한다.
  KB (brain.db BM25) + wiki (markdown) + MEMORY (실시간 보조)
  → 에이전트 간 동일한 진행 상황 + 동일한 방향/목표 공유

주요 기능:
  read_context(topic, agent)     : 에이전트 실행 전 공유 컨텍스트 수집
  write_progress(agent, summary) : 에이전트 완료 후 진행 상황 공유 기록
  get_sprint_state()             : 현재 스프린트 전체 진행 상황
  read_wiki_relevant(topic)      : wiki에서 관련 페이지 검색
  update_wiki_index()            : wiki index.md 갱신

공유 자원 접근 전략:
  brain.db    → read-only URI (동시 읽기 무제한)
  wiki 파일   → 읽기 전용 (쓰기는 nova_kb_wiki_bridge.py)
  MEMORY.md   → 읽기 전용 (쓰기는 memory_slim.py)
  sprint.json → 읽기/쓰기 (에이전트 진행 상황 공유)
"""
from __future__ import annotations

import os, sys, re, json, sqlite3, hashlib, fcntl, tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ── 환경 ─────────────────────────────────────────────────────────────────────
HERMES_HOME  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
NOVA_HOME    = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova"))).expanduser()
BRAIN_DB     = NOVA_HOME / "brain.db"
KB_ROOT      = HERMES_HOME / "kb"
WIKI_DIR     = NOVA_HOME / "wiki"
MEMORY_FILE  = HERMES_HOME / "memories" / "MEMORY.md"
SPRINT_FILE  = NOVA_HOME / "logs" / "sprint_state.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 공유 컨텍스트 읽기 (모든 에이전트의 출발점)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def read_context(topic: str, agent: str = "", max_chars: int = 3000) -> str:
    """
    에이전트 실행 전 공유 지식베이스에서 컨텍스트 수집.
    KB + wiki + MEMORY + 스프린트 진행 상황 → 단일 컨텍스트 문자열.
    모든 에이전트가 이 함수로 동일한 출발점에서 시작.
    """
    parts: list[str] = []
    budget = max_chars

    # ① 스프린트 진행 상황 (가장 중요 — 목표/방향 공유)
    sprint = _read_sprint_state()
    if sprint:
        goal    = sprint.get("goal", "")
        done_ag = sprint.get("completed_agents", [])
        cur_ag  = sprint.get("current_agent", "")
        kpi     = sprint.get("kpi_criteria", [])
        sprint_text = (
            f"=== 현재 스프린트 목표 ===\n{goal}\n"
            f"KPI 기준: {'; '.join(kpi)}\n"
            f"완료된 에이전트: {done_ag}\n"
            f"현재 에이전트: {cur_ag or agent}"
        )
        parts.append(sprint_text)
        budget -= len(sprint_text)

    # ② brain.db BM25 키워드 검색 (읽기 전용 URI)
    if budget > 500:
        kb_text = _search_brain_db(topic, max_chars=min(budget, 1200))
        if kb_text:
            parts.append(f"=== KB 관련 지식 ===\n{kb_text}")
            budget -= len(kb_text)

    # ③ wiki 관련 페이지 검색
    if budget > 300:
        wiki_text = _search_wiki(topic, max_chars=min(budget, 600))
        if wiki_text:
            parts.append(f"=== Wiki 관련 문서 ===\n{wiki_text}")
            budget -= len(wiki_text)

    # ④ 이전 에이전트 handoff (직전 에이전트 결과)
    if budget > 200:
        handoff_text = _read_recent_handoffs(n=3, max_chars=min(budget, 600))
        if handoff_text:
            parts.append(f"=== 이전 에이전트 결과 ===\n{handoff_text}")
            budget -= len(handoff_text)

    # ⑤ MEMORY.md (실시간 보조 — 마지막)
    if budget > 100 and MEMORY_FILE.exists():
        try:
            mem = MEMORY_FILE.read_text(encoding="utf-8", errors="ignore")[:min(budget, 400)]
            parts.append(f"=== MEMORY (실시간 보조) ===\n{mem}")
        except Exception:
            pass

    # ⑥ 시스템 감사 지시서 (nova-sysaudit 완료 후 이슈가 있을 때만)
    audit_directive = NOVA_HOME / "workspace" / "system_audit" / "sprint_directive.md"
    if budget > 100 and audit_directive.exists():
        try:
            ad = audit_directive.read_text(encoding="utf-8", errors="ignore")[:min(budget, 500)]
            # AUDIT_PASS 이외의 이슈가 있을 때만 컨텍스트에 포함
            if ad.strip() and "AUDIT_PASS" not in ad[:100]:
                parts.append(f"=== 시스템 감사 수정 지시 ===\n{ad}")
        except Exception:
            pass

    return "\n\n".join(parts)


def _search_brain_db(topic: str, max_chars: int = 1200) -> str:
    """brain.db page_chunks BM25 키워드 검색 (읽기 전용 URI, 동시 접근 안전)"""
    try:
        uri  = f"file:{BRAIN_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        keywords = [w.lower() for w in topic.split() if len(w) > 2][:5]
        if not keywords:
            conn.close()
            return ""
        kw_cond = " OR ".join(f"content LIKE '%{k}%'" for k in keywords)
        rows = conn.execute(
            f"SELECT section, content FROM page_chunks WHERE {kw_cond} LIMIT 6"
        ).fetchall()
        conn.close()
        lines = []
        total = 0
        for section, content in rows:
            snippet = f"[{section or 'KB'}] {(content or '')[:200]}"
            if total + len(snippet) > max_chars:
                break
            lines.append(snippet)
            total += len(snippet)
        return "\n".join(lines)
    except Exception:
        return ""


def _search_wiki(topic: str, max_chars: int = 600) -> str:
    """wiki 디렉토리에서 관련 페이지 키워드 검색"""
    if not WIKI_DIR.exists():
        return ""
    keywords = [w.lower() for w in topic.split() if len(w) > 2][:4]
    if not keywords:
        return ""
    results = []
    total   = 0
    for md in list(WIKI_DIR.rglob("*.md"))[:80]:
        if md.name in ("index.md", "log.md", "SCHEMA.md"):
            continue
        try:
            text  = md.read_text(encoding="utf-8", errors="ignore")
            score = sum(1 for k in keywords if k in text.lower())
            if score > 0:
                # frontmatter에서 title 추출
                m = re.search(r'^title:\s*(.+)$', text, re.MULTILINE)
                title   = m.group(1).strip() if m else md.stem
                snippet = f"[wiki:{title}] score={score}"
                # 첫 의미 있는 문장
                lines = [l.strip() for l in text.split('\n')
                         if l.strip() and not l.startswith('#')
                         and not l.startswith('---') and not l.startswith('*')]
                if lines:
                    snippet += f" — {lines[0][:120]}"
                results.append((score, snippet))
        except Exception:
            pass
    results.sort(reverse=True)
    lines = []
    for _, s in results[:5]:
        if total + len(s) > max_chars:
            break
        lines.append(s)
        total += len(s)
    return "\n".join(lines)


def _read_recent_handoffs(n: int = 3, max_chars: int = 600) -> str:
    """workspace handoff.json → 이전 에이전트 결과 수집"""
    ws = NOVA_HOME / "workspace"
    if not ws.exists():
        return ""
    handoffs = sorted(ws.rglob("handoff.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    lines = []
    total = 0
    for hf in handoffs:
        try:
            d   = json.loads(hf.read_text())
            ag  = d.get("from_agent", "")
            sm  = d.get("output_summary", "")[:200]
            ok  = d.get("ok", False)
            ln  = f"[{ag} {'✓' if ok else '✗'}] {sm}"
            if total + len(ln) > max_chars:
                break
            lines.append(ln)
            total += len(ln)
        except Exception:
            pass
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 스프린트 진행 상황 (에이전트 간 방향 공유)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _read_sprint_state() -> dict:
    if SPRINT_FILE.exists():
        try:
            return json.loads(SPRINT_FILE.read_text())
        except Exception:
            pass
    return {}


def write_progress(agent: str, status: str, summary: str = "",
                   ok: bool = True) -> None:
    """
    에이전트 완료 후 스프린트 진행 상황 공유 기록.
    다음 에이전트가 read_context()로 이 정보를 읽어 같은 방향으로 진행.
    """
    SPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = _read_sprint_state()

    # 완료 에이전트 목록 갱신
    done = state.get("completed_agents", [])
    if ok and agent not in done:
        done.append(agent)
    state["completed_agents"] = done
    state["current_agent"]    = agent
    state["last_update"]      = datetime.now(timezone.utc).isoformat()

    # 에이전트별 결과 기록
    progress = state.get("progress", {})
    progress[agent] = {
        "status":  status,
        "ok":      ok,
        "summary": summary[:300],
        "at":      datetime.now(timezone.utc).isoformat(),
    }
    state["progress"] = progress

    try:
        # BUG-E1 수정: fcntl lock + atomic rename으로 TOCTOU 레이스 방지
        # 병렬 에이전트(canary+health, retro+document 쌍) 동시 write_progress 시 유실 방지
        lock_file = SPRINT_FILE.with_suffix(".lock")
        with open(lock_file, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            # lock 획득 후 최신 상태 재읽기 (다른 에이전트의 write 반영)
            state = _read_sprint_state()
            done = state.get("completed_agents", [])
            if ok and agent not in done:
                done.append(agent)
            state["completed_agents"] = done
            state["current_agent"]    = agent
            state["last_update"]      = datetime.now(timezone.utc).isoformat()
            progress = state.get("progress", {})
            progress[agent] = {
                "status":  status,
                "ok":      ok,
                "summary": summary[:300],
                "at":      datetime.now(timezone.utc).isoformat(),
            }
            state["progress"] = progress
            # 원자적 쓰기: tmp → rename (POSIX 보장)
            tmp = SPRINT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SPRINT_FILE)
    except Exception:
        pass


def init_sprint(goal: str, kpi_criteria: list[str]) -> None:
    """새 스프린트 시작 시 목표/KPI 기준 초기화.
    이전 스프린트의 kpi_report.md도 삭제 — 잔존 KPI_PASS로 루프가 즉시 종료되는 것 방지.
    """
    SPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "goal":              goal,
        "kpi_criteria":      kpi_criteria,
        "started_at":        datetime.now(timezone.utc).isoformat(),
        "completed_agents":  [],
        "current_agent":     "",
        "progress":          {},
    }
    SPRINT_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 이전 스프린트 kpi_report.md 삭제 (잔존 KPI_PASS 방지)
    kpi_rpt = NOVA_HOME / "workspace" / "kpi_evaluate" / "kpi_report.md"
    if kpi_rpt.exists():
        try:
            kpi_rpt.unlink()
        except Exception:
            pass


def get_sprint_state() -> dict:
    return _read_sprint_state()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NOVA 공유 지식베이스")
    parser.add_argument("--context",  type=str, help="컨텍스트 조회 (topic)")
    parser.add_argument("--agent",    type=str, default="")
    parser.add_argument("--sprint",   action="store_true", help="스프린트 상태")
    parser.add_argument("--init",     type=str, help="스프린트 초기화 (goal)")
    parser.add_argument("--kpi",      nargs="+", default=[], help="KPI 기준")
    args = parser.parse_args()

    if args.context:
        ctx = read_context(args.context, agent=args.agent)
        print(ctx)
    elif args.sprint:
        print(json.dumps(get_sprint_state(), ensure_ascii=False, indent=2))
    elif args.init:
        init_sprint(args.init, args.kpi or ["목표 달성 확인"])
        print(f"스프린트 초기화: {args.init}")
