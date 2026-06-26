from pathlib import Path
import os
#!/usr/bin/env python3
"""
nova_calibration.py — 에이전트 판정력 측정 (GBrain calibration)

bet Takes의 outcome 기반 Brier Score 계산:
  Brier = (weight - outcome)^2  (0=완벽, 1=최악)
  
에이전트별 calibration score = 평균 Brier Score
낮을수록 예측이 정확함

사용:
  python3 nova_calibration.py           # 전체 에이전트 캘리브레이션
  python3 nova_calibration.py --agent nova-qa
"""
import sys, argparse
sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_brain import NovaBrain
from datetime import datetime, timezone


def compute_calibration(agent: str = None) -> dict:
    brain = NovaBrain()
    try:
        query = """
            SELECT holder, weight, outcome, brier_score
            FROM takes
            WHERE kind='bet' AND outcome IS NOT NULL
              AND superseded_by IS NULL
        """
        params = []
        if agent:
            query += " AND holder=?"
            params.append(agent)

        rows = brain.conn.execute(query, params).fetchall()

        # 에이전트별 집계
        from collections import defaultdict
        scores = defaultdict(list)
        for holder, weight, outcome, brier in rows:
            # outcome이 텍스트면 숫자로 변환
            try:
                out_val = float(outcome) if outcome else None
            except (ValueError, TypeError):
                out_val = 1.0 if outcome and outcome.lower() in ('yes','true','correct','pass') else 0.0

            if out_val is not None:
                # Brier score 계산 또는 기존 값 사용
                b = brier if brier is not None else (weight - out_val) ** 2
                scores[holder].append(b)

        result = {}
        for holder, brier_list in scores.items():
            avg = sum(brier_list) / len(brier_list)
            result[holder] = {
                "agent": holder,
                "brier_score_avg": round(avg, 4),
                "n_bets": len(brier_list),
                "calibration_grade": (
                    "A" if avg < 0.1 else
                    "B" if avg < 0.2 else
                    "C" if avg < 0.3 else "D"
                ),
            }

        # brain_health에 calibration 기록
        now = datetime.now(timezone.utc).isoformat()
        import json
        brain.conn.execute("""
            UPDATE brain_health SET notes=? WHERE measured_at=(
                SELECT MAX(measured_at) FROM brain_health
            )
        """, (json.dumps(result),))
        brain.conn.commit()
        return result
    finally:
        brain.close()  # Round6 fix: try/finally ensures close() on all paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent")
    args = parser.parse_args()

    result = compute_calibration(args.agent)
    if not result:
        print("판정된 bet Takes 없음 (outcome 미기록)")
        sys.exit(0)

    print("=== 에이전트 캘리브레이션 ===")
    for holder, d in result.items():
        print(f"  {holder}: Brier={d['brier_score_avg']:.4f} "
              f"[{d['calibration_grade']}] ({d['n_bets']}개 bet)")
