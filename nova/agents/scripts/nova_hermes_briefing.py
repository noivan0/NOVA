#!/usr/bin/env python3
"""
NOVA → 헤르 브리핑 시스템
헤르 세션 시작 시 또는 요청 시 NOVA 현재 상태를 즉시 파악.

역할:
  1. 미읽 이벤트(hermes_events) 조회 → 헤르에게 즉시 보고
  2. NOVA 전체 상태 요약 (brain / kanban / evolution)
  3. "헤르가 지금 알아야 할 것" 우선순위 결정

사용법:
  python3 nova_hermes_briefing.py          # 전체 브리핑
  python3 nova_hermes_briefing.py --events  # 미읽 이벤트만
  python3 nova_hermes_briefing.py --ack    # 모든 이벤트 읽음 처리
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import sqlite3, json, sys, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB           = f"{_HERMES_HOME}/nova_brain.db"
BOARDS_JSON  = f"{_HERMES_HOME}/kanban/nova_boards.json"
PROFILES     = f"{_HERMES_HOME}/profiles"
MEMORY_MD    = Path.home() / ".hermes" / "memories" / "MEMORY.md"
MEMORY_LIMIT = 20000


def get_unread_events() -> list[dict]:
    db = sqlite3.connect(DB)
    c = db.cursor()
    rows = c.execute(
        "SELECT id, event_type, severity, title, detail, source_agent, created_at "
        "FROM hermes_events WHERE is_read=0 ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return [
        {"id": r[0], "type": r[1], "severity": r[2],
         "title": r[3], "detail": r[4], "source": r[5], "at": r[6]}
        for r in rows
    ]


def ack_all_events():
    """hermes_events 읽음 처리 + 오래된 이벤트 삭제 — DB locked 시 재시도"""
    db = None
    try:
        db = sqlite3.connect(DB, timeout=15)
        db.execute("PRAGMA busy_timeout=10000")
        c = db.cursor()
        now = datetime.now(timezone.utc).isoformat()
        # Mark unread as read
        c.execute("UPDATE hermes_events SET is_read=1, acknowledged_at=? WHERE is_read=0", (now,))
        count = c.rowcount
        # Delete events older than 3 days (prevent accumulation)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()  # R18: 3→7일 (이벤트 이력 추적 개선)
        c.execute("DELETE FROM hermes_events WHERE created_at < ? AND is_read=1", (cutoff,))
        deleted = c.rowcount
        db.commit()
        return count
    except Exception as e:
        return 0  # DB locked 시 조용히 실패 (브리핑 중단 방지)
    finally:
        if db:
            db.close()


def get_nova_status() -> dict:
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA busy_timeout=5000")
    c = db.cursor()

    # brain_health
    health = c.execute(
        "SELECT score_overall, total_pages, pages_with_takes, open_contradictions, score_coverage FROM brain_health ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    takes_total = c.execute("SELECT count(*) FROM takes").fetchone()[0]
    orphan = c.execute("SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'").fetchone()[0]

    # evolution
    db.close()

    PROFILES_DIR = Path(PROFILES)
    high = med = low = nascent = 0
    for ag in PROFILES_DIR.iterdir():
        evo = ag / "evolution.md"
        if not evo.exists(): continue
        m = re.search(r'\*\*레벨:\*\*\s*(HIGH|MEDIUM|LOW|NASCENT)', evo.read_text())
        if m:
            lv = m.group(1)
            if lv == "HIGH": high += 1
            elif lv == "MEDIUM": med += 1
            elif lv == "LOW": low += 1
            else: nascent += 1
    total_agents = high + med + low + nascent

    # kanban boards
    boards_file = Path(BOARDS_JSON)
    boards = json.load(open(boards_file))["boards"] if boards_file.exists() else []
    board_status = {}
    for board in boards:
        db_path = f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db"
        if not Path(db_path).exists(): continue
        try:
            bdb = sqlite3.connect(db_path, timeout=3)
            bc = bdb.cursor()
            stats = bc.execute(
                "SELECT status, count(*) FROM tasks GROUP BY status"
            ).fetchall()
            bdb.close()
            board_status[board] = {s: n for s, n in stats}
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            board_status[board] = {"error": str(e)}

    # MEMORY 사용률
    memory_chars = 0
    memory_pct = 0
    try:
        if MEMORY_MD.exists():
            memory_chars = len(MEMORY_MD.read_text(encoding="utf-8"))
            memory_pct = int(memory_chars * 100 / MEMORY_LIMIT)
    except Exception:
        pass

    return {
        "health_score": health[0] if health else "?",
        "total_pages": health[1] if health else "?",
        "pages_with_takes": health[2] if health else 0,
        "open_contradictions": health[3] if health else 0,  # Codex: 브리핑에 모순 수 표시
        "score_coverage": health[4] if health else None,
        "takes": takes_total,
        "orphan": orphan,
        "evolution": {"HIGH": high, "MEDIUM": med, "LOW": low, "total": total_agents},
        "boards": board_status,
        "memory_chars": memory_chars,
        "memory_pct": memory_pct,
    }


def print_briefing():
    print("=" * 60)
    print(f"NOVA 브리핑 — {datetime.now().strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)

    # 1. 미읽 이벤트
    events = get_unread_events()
    if events:
        print(f"\n🚨 미읽 이벤트 {len(events)}개:")
        sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "INFO": "🟢", "WARN": "🟡"}
        for ev in events:
            icon = sev_icon.get(ev["severity"], "⚪")
            ts = ev["at"][:16].replace("T", " ")
            print(f"  {icon} [{ev['severity']}] {ev['title']}")
            if ev["detail"]:
                print(f"     → {ev['detail'][:80]}")
            print(f"     from: {ev['source']} | {ts}")
    else:
        print("\n✅ 미읽 이벤트 없음")

    # 2. NOVA 상태 요약
    s = get_nova_status()
    print(f"\n📊 NOVA 상태:")
    contra = s.get("open_contradictions", 0)
    contra_str = f" / contra={contra}🟠" if contra > 0 else ""
    tp = s.get("total_pages", "?")
    pwt = s.get("pages_with_takes", 0)
    sc = s.get("score_coverage")
    if sc is not None and isinstance(tp, int) and tp > 0:
        real_pct = (pwt / tp) * 100
        cov_str = f" / coverage={sc:.1f}(×200)/실={real_pct:.1f}%({pwt}/{tp})"
    else:
        cov_str = ""
    print(f"  brain: health={s['health_score']} / takes={s['takes']} / pages={tp} / orphan={s['orphan']}{contra_str}{cov_str}")
    # MEMORY 사용률 표시
    mem_pct = s.get("memory_pct", 0)
    mem_chars = s.get("memory_chars", 0)
    mem_icon = "🔴" if mem_pct >= 85 else ("🟡" if mem_pct >= 70 else "🟢")
    print(f"  MEMORY: {mem_chars}자 / {MEMORY_LIMIT}자 ({mem_pct}%) {mem_icon}"
          + (" ← 슬림화 필요!" if mem_pct >= 85 else ""))
    ev = s["evolution"]
    print(f"  evolution: HIGH={ev['HIGH']}/{ev['total']} ({ev['HIGH']/ev['total']*100:.0f}%)" if ev['total'] else "  evolution: ?")
    # STAGNANT agents — 최신 200개 샘플 기준 (R18: 전체 집계 → 샘플 기준으로 통일)
    try:
        db_ev = sqlite3.connect(DB, timeout=5)
        c_ev = db_ev.cursor()
        # 전체 집계 SQL → 각 에이전트별 최신 200개 샘플로 계산
        agents_with_takes = c_ev.execute(
            "SELECT DISTINCT holder FROM takes WHERE (superseded_by IS NULL OR superseded_by='') "
            "GROUP BY holder HAVING count(*)>=10"
        ).fetchall()
        stagnant = []
        for (holder,) in agents_with_takes:
            sample = c_ev.execute(
                "SELECT weight FROM takes WHERE holder=? AND (superseded_by IS NULL OR superseded_by='') "
                "ORDER BY created_at DESC LIMIT 200", (holder,)
            ).fetchall()
            if not sample:
                continue
            weights_s = [r[0] for r in sample]
            avg_s = sum(weights_s) / len(weights_s)
            hq_pct = sum(1 for w in weights_s if w >= 0.85) * 100.0 / len(weights_s)
            if 0.75 <= avg_s < 0.82 and hq_pct < 15.0:
                stagnant.append((holder, len(weights_s), avg_s, hq_pct))
        stagnant.sort(key=lambda r: r[3])
        db_ev.close()
        if stagnant:
            stag_str = ', '.join(f"{r[0]}({r[3]:.0f}%)".replace('nova-','') for r in stagnant[:3])
            print(f"  evolution stagnant: {stag_str} (low hq-ratio)")
    except Exception:
        pass

    # 3. 보드 상태
    print(f"\n🏃 kanban 보드:")
    for board, stats in s["boards"].items():
        active = sum(stats.get(s, 0) for s in ["running", "todo", "ready"])
        done = stats.get("done", 0)
        blocked = stats.get("blocked", 0)
        running = stats.get("running", 0)
        if running > 0:
            print(f"  ▶ {board}: {running}개 실행 중 / todo={stats.get('todo',0)} / done={done} / blocked={blocked}")
        elif active > 0:
            print(f"  ⏸ {board}: active={active} / done={done} / blocked={blocked}")
        else:
            print(f"  ✅ {board}: Sprint 대기 / done={done}")

    # 4. 핵심 권고
    print(f"\n💡 헤르 권고:")
    if events:
        criticals = [e for e in events if e["severity"] == "CRITICAL"]
        highs = [e for e in events if e["severity"] == "HIGH"]
        if criticals:
            print(f"  🔴 CRITICAL {len(criticals)}개 즉시 확인 필요")
        if highs:
            print(f"  🟠 HIGH {len(highs)}개 확인 권고")
    else:
        print("  시스템 정상. 자율 루프 운영 중.")

    print()



def search_kb(query: str, limit: int = 5) -> list[dict]:
    """
    헤르가 대화 중 관련 KB를 찾을 때 사용
    nova_brain.db pages + takes에서 검색 → 실제 KB 파일 경로 반환
    """
    db = sqlite3.connect(DB)
    c = db.cursor()
    results = []

    # 1. takes에서 claim 검색 (지식 레이어)
    takes_hits = c.execute("""
        SELECT t.claim, t.holder, t.weight, p.path, p.title
        FROM takes t
        LEFT JOIN pages p ON t.page_id = p.id
        WHERE t.claim LIKE ?
        ORDER BY t.weight DESC
        LIMIT ?
    """, (f"%{query}%", limit)).fetchall()

    for claim, holder, weight, path, title in takes_hits:
        kb_path = f"{_HERMES_HOME}/kb/{path}" if path else None
        exists = os.path.exists(kb_path) if kb_path else False
        results.append({
            "type": "take",
            "claim": claim[:100],
            "holder": holder,
            "weight": weight,
            "kb_path": kb_path if exists else None,
            "title": str(title)[:60] if title else None,
        })

    # 2. pages title 검색 (인덱스 레이어)
    if len(results) < limit:
        page_hits = c.execute("""
            SELECT path, title, page_type, agent
            FROM pages
            WHERE title LIKE ? AND path != ''
            ORDER BY updated_at DESC
            LIMIT ?
        """, (f"%{query}%", limit - len(results))).fetchall()

        for path, title, pt, agent in page_hits:
            kb_path = f"{_HERMES_HOME}/kb/{path}"
            if os.path.exists(kb_path):
                results.append({
                    "type": "page",
                    "title": str(title)[:60],
                    "page_type": pt,
                    "agent": agent,
                    "kb_path": kb_path,
                })

    db.close()
    return results

def print_kb_search(query: str):
    print(f"\n=== KB 검색: '{query}' ===")
    results = search_kb(query)
    if not results:
        print("  결과 없음")
        return
    for r in results:
        if r["type"] == "take":
            print(f"  [{r['holder']}] w={r['weight']} | {r['claim'][:70]}")
            if r["kb_path"]:
                print(f"    → {r['kb_path']}")
        else:
            print(f"  [{r['page_type']}] {r['title']}")
            print(f"    → {r['kb_path']}")

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--events" in args:
        events = get_unread_events()
        print(json.dumps(events, ensure_ascii=False, indent=2))
    elif "--ack" in args:
        n = ack_all_events()
        print(f"✅ {n}개 이벤트 읽음 처리")
    elif "--search" in args:
        idx = args.index("--search")
        query = args[idx+1] if idx+1 < len(args) else ""
        if query:
            print_kb_search(query)
        else:
            print("사용법: nova_hermes_briefing.py --search <검색어>")
    else:
        print_briefing()
        if "--ack" not in args:
            n = ack_all_events()
            if n > 0:
                print(f"(이벤트 {n}개 읽음 처리됨)")

