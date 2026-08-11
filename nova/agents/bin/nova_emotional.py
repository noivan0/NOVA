import os
from pathlib import Path
#!/usr/bin/env python3
"""
nova_emotional.py — 페이지 감정 가중치 계산 (GBrain emotional_weight)

페이지의 중요도를 0~1로 수치화:
  - Takes weight 평균 (0.5)
  - 중요 태그 존재 (0.3)
  - 연결 밀도: 다른 페이지에서 얼마나 참조되는가 (0.2)

검색 시 emotional_weight 높은 페이지 우선 표시

사용:
  python3 nova_emotional.py           # 전체 재계산
  python3 nova_emotional.py --top 10  # 상위 10개 출력
"""
import sys, re, argparse
sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_brain import NovaBrain
from pathlib import Path

KB_ROOT = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"

IMPORTANT_TAGS = {
    "CRITICAL", "HIGH", "기록", "로드맵", "자율성장", "핵심", "결정",
    "KRAYT", "NOVA", "death-mode", "완료", "확정", "원칙"
}


def compute_emotional_weights() -> int:
    brain = NovaBrain()

    # emotional_weight 컬럼 추가 (없으면)
    try:
        brain.conn.execute("ALTER TABLE pages ADD COLUMN emotional_weight REAL DEFAULT 0.5")
        brain.conn.commit()
    except Exception:
        pass  # 이미 있음

    try:
        pages = brain.conn.execute(
            "SELECT id, path, compiled_truth, page_type FROM pages"
        ).fetchall()

        updated = 0
        for page_id, path, ct, page_type in pages:
            score = 0.0

            # 1. Takes weight 기여 (50%)
            takes = brain.conn.execute(
                "SELECT AVG(weight) FROM takes WHERE page_id=? AND superseded_by IS NULL",
                (page_id,)
            ).fetchone()[0]
            if takes:
                score += takes * 0.5
            else:
                score += 0.25  # 기본값

            # 2. 중요 태그 기여 (30%)
            ct_text = (ct or "").upper()
            tag_hits = sum(1 for tag in IMPORTANT_TAGS if tag.upper() in ct_text)
            score += min(tag_hits / max(len(IMPORTANT_TAGS), 1), 1.0) * 0.3

            # 3. 경로 기반 중요도 (20%)
            if "agents" in path:
                score += 0.2  # 에이전트 결과 최고
            elif "projects" in path:
                score += 0.15
            elif "config" in path:
                score += 0.1
            else:
                score += 0.05

            final = min(round(score, 4), 1.0)
            brain.conn.execute(
                "UPDATE pages SET emotional_weight=? WHERE id=?",
                (final, page_id)
            )
            updated += 1

        brain.conn.commit()
        return updated
    finally:
        brain.close()  # Round6 fix: try/finally ensures close() on all paths


def show_top(n: int = 10):
    brain = NovaBrain()
    try:
        rows = brain.conn.execute(
            "SELECT path, emotional_weight, page_type FROM pages "
            "ORDER BY emotional_weight DESC LIMIT ?", (n,)
        ).fetchall()
        print(f"=== 감정 가중치 Top {n} ===")
        for path, w, pt in rows:
            print(f"  [{w:.3f}] {path} ({pt})")
    except Exception as e:
        print(f"오류: {e}")
    brain.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=0)
    args = parser.parse_args()

    n = compute_emotional_weights()
    print(f"emotional_weight 계산 완료: {n}개 페이지")

    if args.top > 0:
        show_top(args.top)
