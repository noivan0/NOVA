#!/usr/bin/env python3
"""nova_kb_claim_extract.py — 미커버 KB 파일에서 LLM claim 추출 → takes 생성

DreamCycle에서 또는 brain_watcher 주기체크에서 호출.
최근 30일 내 미커버 kb/ 페이지를 배치로 처리.
"""
import sys, re, uuid, sqlite3, datetime
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".hermes" / "bin"))

from nova_llm import call_llm

DB = str(Path.home() / ".hermes" / "nova_brain.db")
MAX_LLM_PER_RUN = 20  # 1회 실행당 LLM 처리 최대 수
FALLBACK_WEIGHT = 0.74  # LLM 실패 시 fallback weight

def run(dry_run: bool = False):
    db = sqlite3.connect(DB)
    c = db.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    # 미커버 kb/ 페이지 (최근 30일, compiled_truth 있는 것 우선)
    candidates = c.execute("""
        SELECT p.id, p.path, p.title, p.compiled_truth
        FROM pages p
        WHERE p.path LIKE 'kb/%'
        AND NOT EXISTS (SELECT 1 FROM takes t WHERE t.page_id=p.id AND t.superseded_by IS NULL)
        AND p.updated_at >= ?
        ORDER BY (p.compiled_truth IS NOT NULL) DESC, p.updated_at DESC
        LIMIT ?
    """, (cutoff, MAX_LLM_PER_RUN)).fetchall()

    llm_cnt = 0
    fallback_cnt = 0
    for page_id, path, title, compiled in candidates:
        if dry_run:
            print(f"  [DRY] {path[-50:]}")
            continue
        claim = None
        if compiled and len(compiled) > 100:
            snippet = compiled[:400]
            prompt = (f"다음 KB 문서에서 핵심 지식 1문장(claim)을 추출하세요.\n"
                      f"JSON으로만: {{\"claim\": \"...\"}}")
            result = call_llm(f"{prompt}\n문서: {title}\n내용: {snippet}", max_tokens=100)
            m = re.search(r'"claim"\s*:\s*"([^"]+)"', result)
            if m:
                claim = m.group(1)[:200]
                llm_cnt += 1

        if not claim:
            agent = path.split("/")[2] if path.count("/") >= 2 else "system"
            fn = path.split("/")[-1].replace(".md","")[:60]
            claim = f"KB 문서: {str(title or fn)[:80]} ({agent})"
            fallback_cnt += 1

        holder = path.split("/")[2] if path.count("/") >= 2 and path.split("/")[2].startswith("nova-") else "nova-evaluator"
        weight = 0.87 if llm_cnt and claim and len(claim) > 20 else FALLBACK_WEIGHT
        c.execute(
            "INSERT INTO takes (page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (page_id, "fact", holder, claim, weight, now, now)
        )

    db.commit()
    remaining = c.execute("""
        SELECT count(*) FROM pages p WHERE p.path LIKE 'kb/%'
        AND NOT EXISTS (SELECT 1 FROM takes t WHERE t.page_id=p.id AND t.superseded_by IS NULL)
    """).fetchone()[0]
    db.close()
    print(f"  kb_claim_extract: LLM={llm_cnt} fallback={fallback_cnt} 남은={remaining}")
    return llm_cnt + fallback_cnt

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)
