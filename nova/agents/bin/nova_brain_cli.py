#!/usr/bin/env python3
"""
nova_brain_cli.py — NOVA Brain 통합 CLI

사용:
  python3 nova_brain_cli.py search "KRAYT security testing"
  python3 nova_brain_cli.py health
  python3 nova_brain_cli.py takes add nova-qa projects/krayt.md take "KRAYT covers OWASP Top10" 0.9
  python3 nova_brain_cli.py takes list --holder nova-qa
  python3 nova_brain_cli.py contradictions detect
  python3 nova_brain_cli.py trajectory add agents/nova-evaluator/index.md krayt_runs 578
  python3 nova_brain_cli.py convert kb/projects/foo.md   # 기존 파일 → CT+TL 구조 변환
  python3 nova_brain_cli.py stats
  python3 nova_brain_cli.py watchdog                     # 헬스 감시 (임계값 초과 시 경고)
"""
import os
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_brain import NovaBrain, parse_kb_file

KB_ROOT = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
BRAIN   = None


def get_brain() -> NovaBrain:
    global BRAIN
    if not BRAIN:
        BRAIN = NovaBrain()
    return BRAIN


# ── 검색 ─────────────────────────────────────────
def cmd_search(args):
    query = " ".join(args.query)
    results = get_brain().search(query, top_k=args.top_k,
                                 section=args.section, agent=args.agent)
    if not results:
        print("결과 없음")
        return
    for i, r in enumerate(results, 1):
        agent = f"[{r['agent']}]" if r.get('agent') else ""
        section = f"({r['section']})" if r.get('section') else ""
        print(f"{i}. [{r['score']:.3f}] {r['title']} {agent} {section}")
        print(f"   {r['path']}")
        print(f"   {r['content'][:150].strip()}...")
        print()


# ── 헬스 ─────────────────────────────────────────
def cmd_health(args):
    brain = get_brain()
    h = brain.measure_health()
    sc = h['score_coverage']
    tp = h['total_pages']
    pwt = h['pages_with_takes']
    real_pct = (pwt / max(tp, 1)) * 100
    print(f"=== NOVA Brain Health ===")
    print(f"Overall: {h['score_overall']}/100")
    print(f"  Coverage (Takes):  {sc:.1f}(내부×200) / 실커버리지={real_pct:.1f}%({pwt}/{tp})")
    print(f"  Freshness:         {h['score_freshness']:.1f}")
    print(f"  Consistency:       {h['score_consistency']:.1f}")
    print(f"  Depth:             {h['score_depth']:.1f}")
    print(f"Pages: {tp} | Takes: {pwt} | Orphans: {h['orphan_pages']}")
    print(f"Contradictions open: {h['open_contradictions']} | Stale: {h['stale_pages']}")
    alerts = json.loads(h['thresholds_crossed'] or '[]')
    if alerts:
        print(f"\n⚠ THRESHOLD ALERTS:")
        for a in alerts:
            print(f"  - {a}")
    else:
        print("\n✓ 모든 임계값 정상")


# ── Takes ─────────────────────────────────────────
def cmd_takes(args):
    brain = get_brain()
    if args.takes_cmd == "add":
        holder, page_path, kind, claim = args.holder, args.page_path, args.kind, args.claim
        weight = float(args.weight) if hasattr(args, 'weight') and args.weight else 0.5
        tid = brain.add_take(holder, page_path, kind, claim, weight,
                              source=getattr(args, 'source', None))
        print(f"✓ Take 추가됨: {tid}")
        print(f"  {kind} [{weight}] by {holder}: {claim}")

    elif args.takes_cmd == "list":
        takes = brain.get_takes(
            page_path=getattr(args, 'page_path', None),
            holder=getattr(args, 'holder', None),
            kind=getattr(args, 'kind', None),
            active_only=not getattr(args, 'all', False)
        )
        if not takes:
            print("Takes 없음")
            return
        for t in takes:
            superseded = " [SUPERSEDED]" if t.get('superseded_by') else ""
            print(f"[{t['kind']}] [{t['weight']:.1f}] {t['holder']}: {t['claim']}{superseded}")
            if t.get('outcome'):
                print(f"  → 결과: {t['outcome']} (Brier: {t.get('brier_score', 'N/A')})")

    elif args.takes_cmd == "resolve":
        brain.resolve_take(args.take_id, args.outcome,
                           float(args.brier) if getattr(args, 'brier', None) else None)
        print(f"✓ Take {args.take_id} 판정: {args.outcome}")


# ── 모순 감지 ─────────────────────────────────────
def cmd_contradictions(args):
    brain = get_brain()
    if args.contradiction_cmd == "detect":
        found = brain.detect_contradictions(top_k_pairs=args.limit)
        print(f"모순 감지: {found}개" if isinstance(found, int) else f"모순 감지: {len(found)}개")
        if isinstance(found, list):
            for c in found[:10]:
                print(f"  [{c['severity']}] {c['path_a']}")
                print(f"         ↔ {c['path_b']}")

    elif args.contradiction_cmd == "list":
        rows = brain.conn.execute("""
            SELECT c.id, p1.path, p2.path, c.severity, c.status, c.detected_at
            FROM contradictions c
            JOIN pages p1 ON c.page_id_a = p1.id
            JOIN pages p2 ON c.page_id_b = p2.id
            WHERE c.status=?
            ORDER BY c.detected_at DESC LIMIT ?
        """, (args.status, args.limit)).fetchall()
        print(f"모순 목록 ({args.status}):")
        for r in rows:
            print(f"  [{r[3]}] {r[1]} ↔ {r[2]}")


# ── 궤적 추적 ─────────────────────────────────────
def cmd_trajectory(args):
    brain = get_brain()
    if args.traj_cmd == "add":
        brain.record_metric(args.page_path, args.metric, float(args.value),
                            unit=getattr(args, 'unit', None))
        print(f"✓ 메트릭 기록: {args.metric}={args.value} ({args.page_path})")

    elif args.traj_cmd == "show":
        data = brain.get_trajectory(args.page_path, args.metric,
                                    days=getattr(args, 'days', 30))
        if not data:
            print("데이터 없음")
            return
        print(f"=== {args.metric} 궤적 ({args.page_path}) ===")
        for d in data:
            print(f"  {d['period']}: {d['value']} {d.get('unit','')}")


# ── KB 파일 구조 변환 (기존 → CT+Timeline) ───────
def cmd_convert(args):
    """기존 KB 파일에 Compiled Truth / Timeline 구조 추가"""
    path = Path(args.path)
    if not path.exists():
        print(f"파일 없음: {path}")
        return

    content = path.read_text(encoding="utf-8")

    # 이미 CT 구조면 스킵
    if "## Compiled Truth" in content or "## Timeline" in content:
        print(f"이미 CT 구조: {path}")
        return

    # 프론트매터 보존
    frontmatter = ""
    body = content
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end > 0:
            frontmatter = content[:end+4] + "\n\n"
            body = content[end+4:].strip()

    # 변환
    converted = f"""{frontmatter}## Compiled Truth

{body}

## Timeline

> 이 섹션은 추가전용입니다. 기존 항목을 수정하지 마세요.

- {__import__('datetime').date.today().isoformat()}: [nova_brain] CT+Timeline 구조로 변환됨
"""

    if args.dry_run:
        print(f"[DRY-RUN] 변환 결과 미리보기:")
        print(converted[:500])
    else:
        path.write_text(converted, encoding="utf-8")
        print(f"✓ 변환 완료: {path}")

        # nova_brain 재인덱싱
        brain = get_brain()
        try:
            rel = str(path.relative_to(KB_ROOT))
        except ValueError:
            rel = str(path)
        brain.index_kb_file(rel, embed=False)
        print(f"  nova_brain 재인덱싱 완료")


# ── Watchdog (헬스 감시) ──────────────────────────
def cmd_watchdog(args):
    """임계값 초과 시 경고 출력 (no_agent 크론 패턴)"""
    brain = get_brain()
    h = brain.measure_health()
    alerts = json.loads(h['thresholds_crossed'] or '[]')

    if alerts:
        # 경고 있을 때만 출력 (no_agent=True 크론: stdout 있으면 전송)
        print(f"⚠ NOVA Brain 임계값 초과 ({h['measured_at'][:10]})")
        for a in alerts:
            print(f"  - {a}")
        print(f"Overall: {h['score_overall']}/100 | Contradictions: {h['open_contradictions']}")
    # 경고 없으면 아무것도 출력 안 함 (no_agent silent pattern)


# ── 통계 ─────────────────────────────────────────
def cmd_stats(args):
    brain = get_brain()
    s = brain.stats()
    print("=== NOVA Brain Statistics ===")
    for k, v in s.items():
        print(f"  {k}: {v}")


# ── 파서 ─────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(prog="nova_brain_cli")
    sub = p.add_subparsers(dest="cmd")

    # search
    ps = sub.add_parser("search")
    ps.add_argument("query", nargs="+")
    ps.add_argument("--top-k", type=int, default=5)
    ps.add_argument("--section", choices=["compiled_truth", "timeline"])
    ps.add_argument("--agent")

    # health
    sub.add_parser("health")

    # takes
    pt = sub.add_parser("takes")
    tsub = pt.add_subparsers(dest="takes_cmd")
    ta = tsub.add_parser("add")
    ta.add_argument("holder"); ta.add_argument("page_path")
    ta.add_argument("kind", choices=["fact","take","bet","hunch"])
    ta.add_argument("claim"); ta.add_argument("weight", nargs="?", default="0.5")
    ta.add_argument("--source")
    tl = tsub.add_parser("list")
    tl.add_argument("--holder"); tl.add_argument("--page-path")
    tl.add_argument("--kind"); tl.add_argument("--all", action="store_true")
    tr = tsub.add_parser("resolve")
    tr.add_argument("take_id"); tr.add_argument("outcome"); tr.add_argument("--brier")

    # contradictions
    pc = sub.add_parser("contradictions")
    csub = pc.add_subparsers(dest="contradiction_cmd")
    cd = csub.add_parser("detect"); cd.add_argument("--limit", type=int, default=50)
    cl = csub.add_parser("list")
    cl.add_argument("--status", default="open")
    cl.add_argument("--limit", type=int, default=20)

    # trajectory
    ptr = sub.add_parser("trajectory")
    trsub = ptr.add_subparsers(dest="traj_cmd")
    tra = trsub.add_parser("add")
    tra.add_argument("page_path"); tra.add_argument("metric"); tra.add_argument("value")
    tra.add_argument("--unit")
    trs = trsub.add_parser("show")
    trs.add_argument("page_path"); trs.add_argument("metric")
    trs.add_argument("--days", type=int, default=30)

    # convert
    pcv = sub.add_parser("convert")
    pcv.add_argument("path"); pcv.add_argument("--dry-run", action="store_true")

    # watchdog / stats
    sub.add_parser("watchdog")
    sub.add_parser("stats")

    args = p.parse_args()

    dispatch = {
        "search": cmd_search, "health": cmd_health, "takes": cmd_takes,
        "contradictions": cmd_contradictions, "trajectory": cmd_trajectory,
        "convert": cmd_convert, "watchdog": cmd_watchdog, "stats": cmd_stats,
    }

    if args.cmd in dispatch:
        dispatch[args.cmd](args)
        if BRAIN:
            BRAIN.close()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
