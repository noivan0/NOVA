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
ROUTER_FILE  = NOVA_HOME / "NOVA_ROUTER.md"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 공유 컨텍스트 읽기 (모든 에이전트의 출발점)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def read_context(topic: str, agent: str = "", max_chars: int = 4500) -> str:
    """
    에이전트 실행 전 공유 지식베이스에서 컨텍스트 수집.
    KB + wiki + MEMORY + 스프린트 진행 상황 → 단일 컨텍스트 문자열.
    모든 에이전트가 이 함수로 동일한 출발점에서 시작.
    """
    parts: list[str] = []
    budget = max_chars

    # ⓪ NOVA Router (세션 지도 — 가장 먼저 로드)
    if ROUTER_FILE.exists():
        try:
            router_text = ROUTER_FILE.read_text(encoding="utf-8", errors="ignore")
            _router_section = f"=== NOVA ROUTER ===\n{router_text}"
            parts.append(_router_section)
            budget -= len(_router_section)  # 헤더 포함 전체 차감
        except Exception as _e:
            print(f"[nova_shared_kb] ROUTER 로드 실패: {_e}", flush=True)

    # ⓪-2 NOVA compound memory (state.md — tried/worked/failed)
    try:
        import sys as _sys
        _sys.path.insert(0, str(HERMES_HOME / "bin"))
        from nova_state_manager import context_block as _state_ctx
        state_text = _state_ctx(n=8)
        if state_text and budget > 300:
            parts.append(state_text)
            budget -= len(state_text)
    except Exception as _e:
        print(f"[nova_shared_kb] STATE 로드 실패: {_e}", flush=True)

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

    # ② KB index.md Logic-First 검색 (graph edge retrieval 포함)
    if budget > 500:
        try:
            import importlib.util as _ilu
            _lfs_path = HERMES_HOME / "bin" / "nova_logic_first_search.py"
            if _lfs_path.exists():
                _spec = _ilu.spec_from_file_location("nova_logic_first_search", _lfs_path)
                _lfs_mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_lfs_mod)
                _lfs_result = _lfs_mod.logic_first_search(topic)
                _lfs_lane   = _lfs_result.get("lane", "web")
                _lfs_pages  = _lfs_result.get("pages", [])
                _lfs_expanded = _lfs_result.get("expanded", [])
                _lfs_lines  = []
                if _lfs_pages:
                    _lfs_lines.append(f"[logic_first|lane={_lfs_lane}]")
                    for _p in _lfs_pages[:2]:
                        _snip = (_p.get("snippet") or "")[:200]
                        _lfs_lines.append(f"  [{_p.get('title','?')}] {_snip}")
                if _lfs_expanded:
                    _lfs_lines.append(f"  graph-expanded: {', '.join(_lfs_expanded[:2])}")
                if _lfs_lines:
                    _lfs_text = "\n".join(_lfs_lines)
                    _lfs_section = f"=== KB Index 정밀 검색 ===\n{_lfs_text}"
                    parts.append(_lfs_section)
                    budget -= len(_lfs_section)
        except Exception:
            pass

    # ②-b brain.db BM25 키워드 검색 (읽기 전용 URI)
    if budget > 400:
        kb_text = _search_brain_db(topic, max_chars=min(budget, 1000))
        if kb_text:
            _kb_section = f"=== KB 관련 지식 ===\n{kb_text}"
            parts.append(_kb_section)
            budget -= len(_kb_section)

    # ③ wiki 관련 페이지 검색
    if budget > 300:
        wiki_text = _search_wiki(topic, max_chars=min(budget, 600))
        if wiki_text:
            _wiki_section = f"=== Wiki 관련 문서 ===\n{wiki_text}"
            parts.append(_wiki_section)
            budget -= len(_wiki_section)

    # ④ 이전 에이전트 handoff (직전 에이전트 결과)
    if budget > 200:
        handoff_text = _read_recent_handoffs(n=3, max_chars=min(budget, 600))
        if handoff_text:
            _hoff_section = f"=== 이전 에이전트 결과 ===\n{handoff_text}"
            parts.append(_hoff_section)
            budget -= len(_hoff_section)

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
    """brain.db 진짜 BM25(pages_fts FTS5) + graph edge retrieval (읽기 전용 URI, 동시 접근 안전)"""
    try:
        uri  = f"file:{BRAIN_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        keywords = [w.lower() for w in topic.split() if len(w) > 2][:5]
        if not keywords:
            conn.close()
            return ""

        # ── 1단계: 진짜 BM25 — pages_fts FTS5 인덱스 활용 ────────────────────
        rows = []
        try:
            match_expr = " OR ".join(keywords)
            fts_rows = conn.execute(
                "SELECT p.path, p.title, pf.compiled_truth "
                "FROM pages_fts pf "
                "JOIN pages p ON pf.rowid = p.rowid "
                "WHERE pages_fts MATCH ? "
                "ORDER BY rank LIMIT 6",
                (match_expr,)
            ).fetchall()
            for path, title, content in fts_rows:
                rows.append((title or path, (content or "")[:200]))
        except Exception:
            pass

        # FTS 결과 빈약 시 page_chunks LIKE 폴백
        # SECURITY-016 (2026-08-18, deep audit round 5): same class of
        # bug as SECURITY-015 (nova_agent_worker.py) -- `keywords` is
        # derived from `topic`, which originates from CLI
        # `--context topic=...` (attacker-controlled). f-string-built
        # LIKE clauses crashed on a single apostrophe (e.g. "don't").
        # Fixed with parameterized placeholders.
        if len(rows) < 2:
            kw_cond = " OR ".join("content LIKE ?" for _ in keywords)
            chunk_rows = conn.execute(
                f"SELECT section, content FROM page_chunks WHERE {kw_cond} LIMIT 6",
                [f"%{k}%" for k in keywords],
            ).fetchall()
            for section, content in chunk_rows:
                rows.append((section or "KB", (content or "")[:200]))

        # ── 2단계: graph edge retrieval — BM25 top hits → connected nodes ─────
        edge_snippets = []
        try:
            kw_path = " OR ".join("path LIKE ?" for _ in keywords)
            seed_pages = conn.execute(
                f"""SELECT id, path, title FROM pages
                    WHERE ({kw_path})
                      AND path NOT LIKE 'kb/agents/%'
                      AND path NOT LIKE 'kb/lessons/%'
                      AND path NOT LIKE 'kb/memory_archive/%'
                    LIMIT 3""",
                [f"%{k}%" for k in keywords],
            ).fetchall()
            seed_ids = [r[0] for r in seed_pages]
            if seed_ids:
                placeholders = ",".join("?" * len(seed_ids))
                connected = conn.execute(
                    f"""SELECT DISTINCT p.path, p.title, e.edge_type, e.weight
                        FROM knowledge_graph_edges e
                        JOIN pages p ON (
                            CASE WHEN e.src_page_id IN ({placeholders})
                                 THEN e.dst_page_id = p.id
                                 ELSE e.src_page_id = p.id
                            END
                        )
                        WHERE e.src_page_id IN ({placeholders}) OR e.dst_page_id IN ({placeholders})
                        ORDER BY e.weight DESC LIMIT 5""",
                    seed_ids + seed_ids + seed_ids
                ).fetchall()
                for path, title, etype, weight in connected:
                    edge_snippets.append(f"[graph:{etype}|w={weight}] {title or path}")
        except Exception:
            pass

        conn.close()

        # ── 3단계: BM25 결과 + graph edges 조합 ─────────────────────────────
        lines = []
        total = 0
        for section, content in rows:
            snippet = f"[{section or 'KB'}] {(content or '')[:200]}"
            if total + len(snippet) > max_chars * 0.7:
                break
            lines.append(snippet)
            total += len(snippet)
        # edge 결과 추가 (나머지 budget)
        if edge_snippets and total < max_chars:
            lines.append("--- graph edges ---")
            for es in edge_snippets:
                if total + len(es) > max_chars:
                    break
                lines.append(es)
                total += len(es)
        return "\n".join(lines)
    except Exception:
        return ""


def _search_wiki(topic: str, max_chars: int = 600) -> str:
    """wiki pages_fts BM25 검색 (파일시스템 직접 접근 대신 brain.db 통합 검색)"""
    try:
        uri  = f"file:{BRAIN_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        keywords = [w.lower() for w in topic.split() if len(w) > 2][:4]
        if not keywords:
            conn.close()
            return ""
        match_expr = " OR ".join(keywords)
        rows = conn.execute(
            "SELECT p.path, p.title, bm25(pages_fts) as score "
            "FROM pages_fts "
            "JOIN pages p ON pages_fts.rowid = p.rowid "
            "WHERE pages_fts MATCH ? "
            "  AND p.path LIKE 'wiki/%' "
            "ORDER BY rank LIMIT 5",
            (match_expr,)
        ).fetchall()
        conn.close()
        lines = []
        total = 0
        for path, title, score in rows:
            snippet = f"[wiki:{title or path}] score={abs(score):.2f}"
            if total + len(snippet) > max_chars:
                break
            lines.append(snippet)
            total += len(snippet)
        return "\n".join(lines)
    except Exception:
        return ""


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
