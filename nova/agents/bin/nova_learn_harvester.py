#!/usr/bin/env python3
"""
nova_learn_harvester.py — NOVA 집단 학습 수집기
gstack /learn 이식: 반복 패턴 감지 + stale takes GC

실행: python3 nova_learn_harvester.py [--gc] [--report]
"""
import sqlite3, sys, json, re
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERMES_HOME = Path.home() / ".hermes"
DB = HERMES_HOME / "nova_brain.db"
KB_LEARNINGS = HERMES_HOME / "kb" / "nova" / "learnings"

def get_con():
    try:
        import sqlite3_vec  # type: ignore
    except ImportError:
        pass
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con

def harvest_patterns():
    """반복 패턴 감지: 같은 에이전트가 같은 종류 claim을 2회 이상 기록 → 구조적 이슈"""
    con = get_con()
    rows = con.execute("""
        SELECT holder, kind, claim, weight, created_at
        FROM takes
        ORDER BY created_at DESC
    """).fetchall()
    
    # claim 앞 60자 기준 클러스터링
    clusters = defaultdict(list)
    for r in rows:
        key = (r["holder"], r["kind"], r["claim"][:50])
        clusters[key].append(dict(r))
    
    repeated = {k: v for k, v in clusters.items() if len(v) >= 2}
    print(f"=== 반복 패턴 감지: {len(repeated)}개 클러스터 ===")
    for (holder, kind, prefix), items in sorted(repeated.items(), key=lambda x: -len(x[1])):
        print(f"  [{holder}] {kind} ×{len(items)}: {prefix[:50]}...")
    
    con.close()
    return repeated

def stale_gc(dry_run=True):
    """stale takes GC: 30일 이상 + weight < 0.3"""
    con = get_con()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    
    stale = con.execute("""
        SELECT id, holder, claim, weight, created_at
        FROM takes
        WHERE created_at < ? AND weight < 0.3
    """, (cutoff,)).fetchall()
    
    print(f"\n=== Stale Takes ({len(stale)}개, 30일↑ + weight<0.3) ===")
    for r in stale[:5]:
        print(f"  {r['holder']}: {r['claim'][:50]!r} (w={r['weight']})")
    
    if not dry_run and stale:
        ids = [r["id"] for r in stale]
        con.execute(f"DELETE FROM takes WHERE id IN ({','.join(['?']*len(ids))})", ids)
        con.commit()
        print(f"  → {len(stale)}개 삭제 완료")
    elif dry_run:
        print("  → dry_run=True, 실제 삭제 없음")
    
    con.close()
    return len(stale)

def export_learnings():
    """최근 7일 학습 요약 → KB 저장"""
    con = get_con()
    recent = con.execute("""
        SELECT holder, kind, claim, weight, created_at
        FROM takes
        WHERE created_at >= datetime('now', '-7 days')
        AND weight >= 0.7
        ORDER BY weight DESC, created_at DESC
        LIMIT 30
    """).fetchall()
    
    KB_LEARNINGS.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_file = KB_LEARNINGS / f"weekly-{today}.md"
    
    lines = [f"# NOVA 주간 학습 요약 — {today}\n"]
    lines.append(f"총 {len(recent)}개 (최근 7일, weight≥0.7)\n")
    
    by_holder = defaultdict(list)
    for r in recent:
        by_holder[r["holder"]].append(r)
    
    for holder, items in sorted(by_holder.items()):
        lines.append(f"\n## {holder} ({len(items)}개)")
        for item in items[:5]:
            lines.append(f"- [{item['weight']:.2f}] {item['claim'][:80]}")
    
    out_file.write_text("\n".join(lines))
    print(f"\n=== 학습 요약 저장: {out_file} ===")
    con.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gc", action="store_true", help="stale takes 삭제")
    parser.add_argument("--report", action="store_true", help="학습 요약 저장")
    args = parser.parse_args()
    
    harvest_patterns()
    stale_gc(dry_run=not args.gc)
    if args.report:
        export_learnings()
