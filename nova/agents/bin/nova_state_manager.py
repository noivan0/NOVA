#!/usr/bin/env python3
"""
nova_state_manager.py — NOVA compound memory (Graph Engineering Step11)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
원문 원칙 (@unicodef1wn, Step11):
  "A state file logs what was tried, what worked, what didn't."
  "Every run ends by writing to it and starts by reading it."
  "The value was never the model. It's the router, the index, the edges,
   the rules: plain markdown in a vault you own."

NOVA state.md 구조:
  ~/.nova/state.md — 세션 간 compound memory
  형식: ## [YYYY-MM-DD HH:MM] 에이전트 | 결과 | 학습
  tried / worked / failed 3구간

사용:
  python3 nova_state_manager.py write --agent nova-dev --result OK --learning "BUG: X 발견"
  python3 nova_state_manager.py read           # 최근 20개 state 읽기
  python3 nova_state_manager.py summary        # tried/worked/failed 통계
  python3 nova_state_manager.py context        # nova_shared_kb 용 컨텍스트 블록
"""
import os, sys, re, sqlite3
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_FILE  = NOVA_HOME / "state.md"
MAX_ENTRIES = 200  # state.md 최대 항목 (초과 시 오래된 것 아카이브)

HEADER = """# NOVA State (compound memory)
> "Every run ends by writing to it and starts by reading it." — Graph Engineering Step11
> 자동 관리: nova_state_manager.py | 직접 편집 금지

"""


def _load() -> list[dict]:
    """state.md → 항목 목록 파싱"""
    if not STATE_FILE.exists():
        return []
    content = STATE_FILE.read_text(encoding="utf-8", errors="replace")
    entries = []
    # ## [YYYY-MM-DD HH:MM] agent | result | learning
    pattern = re.compile(
        r"^## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] ([^|]+)\| ([^|]+)\| (.+)$",
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        entries.append({
            "ts":       m.group(1).strip(),
            "agent":    m.group(2).strip(),
            "result":   m.group(3).strip(),
            "learning": m.group(4).strip(),
        })
    return entries


def _save(entries: list[dict]):
    """항목 목록 → state.md 저장"""
    # 초과 시 오래된 항목 아카이브
    if len(entries) > MAX_ENTRIES:
        archive_dir = NOVA_HOME / "state_archive"
        archive_dir.mkdir(exist_ok=True)
        ts_tag = datetime.now().strftime("%Y%m%d_%H%M")
        archive_file = archive_dir / f"state_{ts_tag}.md"
        archive_content = HEADER
        for e in entries[:-MAX_ENTRIES]:
            archive_content += f"## [{e['ts']}] {e['agent']} | {e['result']} | {e['learning']}\n"
        archive_file.write_text(archive_content)
        entries = entries[-MAX_ENTRIES:]

    content = HEADER
    for e in entries:
        content += f"## [{e['ts']}] {e['agent']} | {e['result']} | {e['learning']}\n"
    STATE_FILE.write_text(content, encoding="utf-8")


def write_state(agent: str, result: str, learning: str):
    """새 state 항목 추가"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entries = _load()
    entries.append({"ts": ts, "agent": agent, "result": result, "learning": learning})
    _save(entries)
    print(f"[state] 기록: [{ts}] {agent} | {result} | {learning[:60]}")


def read_state(n: int = 20) -> list[dict]:
    """최근 N개 state 읽기"""
    entries = _load()
    return entries[-n:]


def summary() -> dict:
    """tried/worked/failed 통계"""
    entries = _load()
    tried  = len(entries)
    worked = sum(1 for e in entries if e["result"].upper() in ("OK", "PASS", "SUCCESS", "WORKED"))
    failed = sum(1 for e in entries if e["result"].upper() in ("FAIL", "ERROR", "FAILED", "KPI_FAIL"))
    by_agent = {}
    for e in entries:
        by_agent[e["agent"]] = by_agent.get(e["agent"], 0) + 1
    return {
        "tried": tried, "worked": worked, "failed": failed,
        "success_rate": f"{worked/tried*100:.0f}%" if tried else "N/A",
        "top_agents": sorted(by_agent.items(), key=lambda x: x[1], reverse=True)[:5],
    }


def context_block(n: int = 10) -> str:
    """nova_shared_kb.py read_context() 용 컨텍스트 블록"""
    entries = read_state(n)
    if not entries:
        return "=== NOVA STATE ===\n(빈 상태)\n"
    lines = ["=== NOVA STATE (compound memory, 최근 " + str(len(entries)) + "개) ==="]
    for e in entries[-n:]:
        lines.append(f"[{e['ts']}] {e['agent']} → {e['result']}: {e['learning'][:80]}")
    lines.append("")
    return "\n".join(lines)


def sync_from_brain_db():
    """brain.db agent_activity → state.md 자동 동기화"""
    try:
        conn = sqlite3.connect(str(NOVA_HOME / "brain.db"), timeout=5)
        # 최근 agent_activity에서 주요 결과 추출
        rows = conn.execute(
            "SELECT agent, action, result, recorded_at FROM agent_activity "
            "WHERE result IS NOT NULL ORDER BY recorded_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[state] brain.db 조회 실패: {e}")
        return 0

    existing_ts = {e["ts"] for e in _load()}
    added = 0
    for row in rows:
        agent, action, result, at = row
        if not result or not at:
            continue
        ts = at[:16].replace("T", " ")  # 2026-08-10T09:00:00 → 2026-08-10 09:00
        if ts in existing_ts:
            continue
        # result 요약
        result_short = result[:40].replace("\n", " ")
        learning = f"{action}: {result_short}" if action else result_short
        entries = _load()
        entries.append({"ts": ts, "agent": agent or "system",
                         "result": ("OK" if any(k in result.upper() for k in ["OK","PASS","DONE","SUCCESS"]) else "FAIL"),
                         "learning": learning[:100]})
        _save(entries)
        existing_ts.add(ts)
        added += 1

    print(f"[state] brain.db 동기화: {added}개 신규 항목")
    return added


def main():
    args = sys.argv[1:]
    if not args or args[0] == "read":
        n = int(args[1]) if len(args) > 1 else 20
        for e in read_state(n):
            print(f"[{e['ts']}] {e['agent']:15s} | {e['result']:8s} | {e['learning'][:60]}")
    elif args[0] == "write":
        agent    = next((args[i+1] for i,a in enumerate(args) if a == "--agent"), "unknown")
        result   = next((args[i+1] for i,a in enumerate(args) if a == "--result"), "OK")
        learning = next((args[i+1] for i,a in enumerate(args) if a == "--learning"), "")
        write_state(agent, result, learning)
    elif args[0] == "summary":
        s = summary()
        print(f"tried={s['tried']}, worked={s['worked']}, failed={s['failed']}, "
              f"success_rate={s['success_rate']}")
        print("top agents:", s['top_agents'])
    elif args[0] == "context":
        print(context_block())
    elif args[0] == "sync":
        sync_from_brain_db()
    else:
        print("사용법: nova_state_manager.py [read|write|summary|context|sync]")


if __name__ == "__main__":
    main()
