import os
#!/usr/bin/env python3
"""
nova_doctor.py — NOVA Brain 자동 수복 (GBrain brain health remediation)

헬스 점수 < target 이면 자동 수복 실행:
  1. CT 내용 부족 페이지 재파싱
  2. Orphan 페이지 page_type 추론 및 수정
  3. Takes 없는 중요 페이지에 Takes 자동 추가 (Haiku)
  4. 벡터 없는 청크 임베딩 (제한적)

사용:
  python3 nova_doctor.py --target-score 75
  python3 nova_doctor.py --target-score 75 --max-calls 20 --dry-run
"""
import sys, os, re, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_llm import call_llm as call_haiku  # nova_llm 공용 헬퍼 사용
from nova_brain import NovaBrain

KB_ROOT = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"


def remediate(target_score: float = 75.0, max_calls: int = 30,
              dry_run: bool = False) -> dict:
    brain = NovaBrain()
    llm_calls = 0
    actions = {"reindex": 0, "orphan_fixed": 0, "takes_added": 0, "embedded": 0}

    print(f"[nova_doctor] target={target_score}, max_calls={max_calls}, dry_run={dry_run}")
    h = brain.measure_health()
    print(f"  현재 헬스: {h['score_overall']}/100")

    if h['score_overall'] >= target_score:
        print("  목표 달성 — 수복 불필요")
        brain.close()
        return actions

    # Step 1: CT 빈 페이지 재파싱
    short_ct = brain.conn.execute("""
        SELECT id, path FROM pages
        WHERE length(compiled_truth) < 50
        LIMIT 50
    """).fetchall()

    for page_id, path in short_ct:
        abs_path = KB_ROOT / path
        if not abs_path.exists():
            continue
        if not dry_run:
            from nova_brain import parse_kb_file
            parsed = parse_kb_file(abs_path)
            if parsed and len(parsed.get("compiled_truth", "")) > 50:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                brain.conn.execute("""
                    UPDATE pages SET compiled_truth=?, timeline=?, updated_at=?
                    WHERE id=?
                """, (parsed["compiled_truth"], parsed.get("timeline",""), now, page_id))
                actions["reindex"] += 1
    brain.conn.commit()
    print(f"  재파싱: {actions['reindex']}개")

    # Step 2: Orphan 페이지 page_type 추론
    orphans = brain.conn.execute("""
        SELECT id, path FROM pages
        WHERE page_type='general' AND agent IS NULL
        LIMIT 100
    """).fetchall()

    type_map = {"projects": "project", "config": "config",
                "fixes": "fix", "agents": "agent", "user": "entity"}
    for page_id, path in orphans:
        inferred = "general"
        for prefix, pt in type_map.items():
            if path.startswith(prefix + "/"):
                inferred = pt
                break
        if inferred != "general":
            if not dry_run:
                brain.conn.execute(
                    "UPDATE pages SET page_type=? WHERE id=?", (inferred, page_id)
                )
            actions["orphan_fixed"] += 1
    brain.conn.commit()
    print(f"  Orphan 수정: {actions['orphan_fixed']}개")

    # Step 3: Takes 없는 중요 페이지에 Takes 자동 추가
    pages_no_takes = brain.conn.execute("""
        SELECT p.id, p.path, p.compiled_truth
        FROM pages p
        LEFT JOIN takes t ON p.id = t.page_id AND t.superseded_by IS NULL
        WHERE t.id IS NULL
          AND p.page_type IN ('project','fix','agent')
          AND length(p.compiled_truth) > 300
        ORDER BY length(p.compiled_truth) DESC
        LIMIT 30
    """).fetchall()

    from datetime import datetime, timezone
    import hashlib
    now = datetime.now(timezone.utc).isoformat()

    for page_id, path, ct in pages_no_takes:
        if llm_calls >= max_calls:
            break
        if not ct:
            continue

        prompt = f"""다음 문서의 핵심 사실을 1-2문장으로 추출하세요 (JSON):
{{"kind":"fact","claim":"...","weight":0.9}}

문서 ({path}):
{ct[:400]}"""
        result = call_haiku(prompt, max_tokens=100)
        llm_calls += 1

        try:
            m = re.search(r'\{[^}]+\}', result, re.DOTALL)
            if m:
                item = json.loads(m.group())
                claim = item.get("claim","")
                if claim and len(claim) > 10:
                    tid = hashlib.sha256(f"doctor:{path}:{claim}".encode()).hexdigest()[:16]
                    if not dry_run:
                        brain.conn.execute("""
                            INSERT OR IGNORE INTO takes
                            (id, page_id, kind, holder, claim, weight, source, created_at)
                            VALUES (?,?,?,?,?,?,?,?)
                        """, (tid, page_id, item.get("kind","fact"),
                              "nova-doctor", claim,
                              float(item.get("weight", 0.9)),
                              f"auto:{path}", now))
                    actions["takes_added"] += 1
        except Exception:
            pass

    brain.conn.commit()
    print(f"  Takes 추가: {actions['takes_added']}개 (LLM calls: {llm_calls})")

    # 최종 헬스 측정
    h2 = brain.measure_health()
    print(f"  수복 후 헬스: {h2['score_overall']}/100")
    brain.close()

    actions["before"] = h["score_overall"]
    actions["after"] = h2["score_overall"]
    return actions


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-score", type=float, default=75.0)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = remediate(args.target_score, args.max_calls, args.dry_run)
    print(f"\n결과: {result}")
