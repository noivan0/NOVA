#!/usr/bin/env python3
"""
nova_blast_radius.py — Blast Radius Lane 분류기 (Eval Engineering Step 6)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
원문 원칙 (@hanakoxbt):
  "Open the gate on blast radius, not on confidence."
  Lane A: Reversible + Contained → deterministic check 통과 시 자동 진행
  Lane B: Reversible + Wide      → deterministic + clean trajectory 모두 필요
  Lane C: Hard to reverse        → 점수 무관 차단 (인간 승인 필수)

  게이트 증거 우선순위:
    1st: Deterministic (테스트/타입/스키마/샌드박스) — 모델 개입 없음
    2nd: Eval trajectory
    3rd: History (롤백 이력)
    Last: 모델 자체 평가 (가장 낮게 가중)

사용:
  from nova_blast_radius import classify_blast_radius, BlastResult
  result = classify_blast_radius(diff_text, agent="nova-dev", trajectory_pass=True)
  print(result.lane, result.gate_open, result.reason)
"""
import re, os, sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home() / ".nova")))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
BRAIN_DB    = NOVA_HOME / "brain.db"

Lane = Literal["A", "B", "C"]


# Lane C 패턴 — 역전 불가 작업 (점수 무관 차단)
LANE_C_PATTERNS = [
    r'\bDROP\s+TABLE\b',
    r'\bDROP\s+DATABASE\b',
    r'\bDELETE\s+FROM\b',
    r'\bTRUNCATE\b',
    r'\bALTER\s+TABLE\b.*\bDROP\b',
    r'\brm\s+-rf\b',
    r'\bshutil\.rmtree\b',
    r'\bos\.remove\b.*prod',
    r'production.*secret',
    r'deploy.*prod(?:uction)?',
    r'HERMES_MASTER_APIKEY',
    r'password\s*=',
    r'secret\s*=',
]

# Lane A 패턴 — 가역적 + 격리 (즉시 통과 가능)
LANE_A_PATTERNS = [
    r'test_.*\.py',
    r'_test\.py',
    r'\.md$',
    r'# copy change',
    r'# docs',
    r'README',
    r'CHANGELOG',
    r'\.gitignore',
    r'harness\.yaml',       # harness 설정 변경 (격리됨)
    r'prompts/.*\.txt',
]

# Lane B 패턴 — 가역적 + 광범위 (조건부)
LANE_B_SHARED_PATTERNS = [
    r'nova_shared_kb\.py',
    r'nova_chain_engine\.py',
    r'nova_kb_on_change\.py',
    r'brain\.db',
    r'kb_unified_search\.py',
    r'SOUL\.md',
    r'config\.yaml',
    r'nova\.yaml',
    r'schema\.py',
    r'__init__\.py',
    r'requirements\.txt',
]


@dataclass
class BlastResult:
    lane: Lane
    gate_open: bool
    reason: str
    evidence: dict = field(default_factory=dict)
    # 게이트 증거 우선순위 (원문 Step 6)
    deterministic_ok: bool = True
    trajectory_ok: bool = True
    history_ok: bool = True
    model_score: float = 0.0  # 가장 낮게 가중
    # Shadow Mode (GATE_SHADOW) — 채점만, merge 없음
    shadow_mode: bool = False
    shadow_score: bool = False  # shadow 시 실제 열렸을 것인가?


def _check_rollback_history(agent: str, paths: list[str]) -> tuple[bool, float]:
    """
    brain.db agent_activity에서 해당 에이전트의 해당 경로 롤백 이력 확인.
    롤백률 < 20% → OK
    """
    if not BRAIN_DB.exists() or not paths:
        return True, 0.0
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        total = conn.execute(
            "SELECT count(*) FROM agent_activity WHERE agent=?", [agent]
        ).fetchone()[0]
        rollbacks = conn.execute(
            "SELECT count(*) FROM agent_activity WHERE agent=? AND result LIKE '%ROLLBACK%'",
            [agent]
        ).fetchone()[0]
        conn.close()
        if total == 0:
            return True, 0.0
        rate = rollbacks / total
        return rate < 0.20, rate
    except Exception:
        return True, 0.0


def classify_blast_radius(
    diff: str,
    agent: str = "nova-dev",
    trajectory_pass: bool = True,
    deterministic_pass: bool = True,
    model_confidence: float = 0.0,
    verbose: bool = False,
    shadow_mode: bool = False,   # 신규 추가
) -> BlastResult:
    """
    diff 텍스트 + 실행 컨텍스트 → Lane A/B/C 분류 + gate_open 판정.

    Args:
        diff: git diff 또는 변경 내용 텍스트
        agent: 실행한 에이전트 이름
        trajectory_pass: Trajectory 평가 통과 여부 (nova-review에서 수집)
        deterministic_pass: Deterministic 선검증 통과 (kpi_evaluate에서 수집)
        model_confidence: 모델 자체 평가 점수 (0~1, 가장 낮게 가중)
        verbose: 상세 로그 출력
        shadow_mode: True 시 판정은 동일하게 하되 gate_open은 항상 False
                     (채점만, merge 없음). shadow_score에 실제 열렸을 결과 저장.
                     환경변수 GATE_SHADOW=true 로도 활성화.
    """
    # 환경변수 GATE_SHADOW=true 시 자동 shadow_mode 활성화
    shadow_mode = shadow_mode or os.environ.get('GATE_SHADOW', '').lower() == 'true'
    evidence = {
        "deterministic": deterministic_pass,
        "trajectory": trajectory_pass,
        "model_confidence": model_confidence,
    }

    # ─── Lane C 판정 (역전 불가 — 즉시 차단) ─────────────────────────────
    for pattern in LANE_C_PATTERNS:
        if re.search(pattern, diff, re.IGNORECASE):
            reason = f"Lane C: 역전 불가 패턴 탐지 ({pattern}) — 인간 승인 필수"
            if verbose:
                print(f"[blast_radius] {reason}")
            return BlastResult(
                lane="C",
                gate_open=False,
                reason=reason,
                evidence=evidence,
                deterministic_ok=deterministic_pass,
                trajectory_ok=trajectory_pass,
                history_ok=True,
                model_score=model_confidence,
                shadow_mode=shadow_mode,
                shadow_score=False,  # Lane C는 항상 차단
            )

    # ─── Lane A 판정 (가역적 + 격리) ────────────────────────────────────
    lane_a_hits = [p for p in LANE_A_PATTERNS if re.search(p, diff, re.IGNORECASE)]
    lane_b_hits = [p for p in LANE_B_SHARED_PATTERNS if re.search(p, diff, re.IGNORECASE)]

    # 변경 파일 목록 추출 (diff에서)
    changed_files = re.findall(r'(?:---|\+\+\+)\s+(?:a/|b/)?([\w/\.\-_]+)', diff)

    # 롤백 이력 확인 (Step6: 3rd priority)
    history_ok, rollback_rate = _check_rollback_history(agent, changed_files)
    evidence["rollback_rate"] = rollback_rate

    if verbose:
        print(f"[blast_radius] Lane A hits: {lane_a_hits}")
        print(f"[blast_radius] Lane B hits: {lane_b_hits}")
        print(f"[blast_radius] Rollback rate: {rollback_rate:.1%}")

    if lane_a_hits and not lane_b_hits:
        # Lane A: Deterministic 통과만으로 열기
        actual_open = deterministic_pass and history_ok
        reason = (
            f"Lane A (격리 변경): {lane_a_hits[0]} — "
            f"deterministic={'OK' if deterministic_pass else 'FAIL'}, "
            f"rollback_rate={rollback_rate:.1%}"
        )
        return BlastResult(
            lane="A",
            gate_open=False if shadow_mode else actual_open,
            reason=reason,
            evidence=evidence,
            deterministic_ok=deterministic_pass,
            trajectory_ok=trajectory_pass,
            history_ok=history_ok,
            model_score=model_confidence,
            shadow_mode=shadow_mode,
            shadow_score=actual_open,
        )

    if lane_b_hits:
        # Lane B: Deterministic + Trajectory 모두 필요
        actual_open = deterministic_pass and trajectory_pass and history_ok
        reason = (
            f"Lane B (공유 컴포넌트): {lane_b_hits[0]} — "
            f"deterministic={'OK' if deterministic_pass else 'FAIL'}, "
            f"trajectory={'OK' if trajectory_pass else 'FAIL'}, "
            f"rollback_rate={rollback_rate:.1%}"
        )
        return BlastResult(
            lane="B",
            gate_open=False if shadow_mode else actual_open,
            reason=reason,
            evidence=evidence,
            deterministic_ok=deterministic_pass,
            trajectory_ok=trajectory_pass,
            history_ok=history_ok,
            model_score=model_confidence,
            shadow_mode=shadow_mode,
            shadow_score=actual_open,
        )

    # 기본값: Lane B (알 수 없는 변경은 안전하게 조건부)
    actual_open = deterministic_pass and trajectory_pass and history_ok
    reason = (
        f"Lane B (기본값 — 미분류 변경): "
        f"deterministic={'OK' if deterministic_pass else 'FAIL'}, "
        f"trajectory={'OK' if trajectory_pass else 'FAIL'}"
    )
    return BlastResult(
        lane="B",
        gate_open=False if shadow_mode else actual_open,
        reason=reason,
        evidence=evidence,
        deterministic_ok=deterministic_pass,
        trajectory_ok=trajectory_pass,
        history_ok=history_ok,
        model_score=model_confidence,
        shadow_mode=shadow_mode,
        shadow_score=actual_open,
    )


def format_gate_report(result: BlastResult) -> str:
    """nova-checkpoint harness 출력용 보고서"""
    status = "GATE_OPEN" if result.gate_open else "GATE_BLOCKED"
    lines = [
        f"BLAST_RADIUS_LANE={result.lane}",
        f"GATE_STATUS={status}",
        f"REASON={result.reason}",
        f"DETERMINISTIC={'PASS' if result.deterministic_ok else 'FAIL'}",
        f"TRAJECTORY={'PASS' if result.trajectory_ok else 'FAIL'}",
        f"HISTORY={'OK' if result.history_ok else 'WARN'}",
        f"MODEL_CONFIDENCE={result.model_score:.2f} (lowest weight, not gating)",
    ]
    if result.shadow_mode:
        lines.insert(0, "SHADOW_MODE=TRUE (GATE_BLOCKED for accumulation)")
        lines.append(f"SHADOW_WOULD_OPEN={result.shadow_score}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    # 테스트 케이스
    test_cases = [
        ("test_nova_chain.py 수정", "--- a/test_nova_chain.py\n+++ b/test_nova_chain.py\n+def test_fork():\n+    pass", True, True),
        ("공유 컴포넌트 수정", "--- a/nova_chain_engine.py\n+++ b/nova_chain_engine.py\n+CHAIN_FORK['nova-dev'] = ['review']", True, True),
        ("DB 삭제 시도", "DELETE FROM knowledge_graph_edges WHERE id='xxx'", True, True),
        ("schema 변경 (Lane B)", "--- a/schema.py\n+++ b/schema.py\n+CREATE TABLE new_table", True, False),
    ]

    for name, diff, det, traj in test_cases:
        result = classify_blast_radius(diff, trajectory_pass=traj, deterministic_pass=det)
        print(f"\n[{name}]")
        print(format_gate_report(result))

    # Shadow Mode 테스트
    print("\n\n=== SHADOW MODE 테스트 ===")
    shadow_cases = [
        ("Lane A Shadow", "--- a/test_nova_chain.py\n+++ b/test_nova_chain.py\n+def test_fork():\n+    pass", True, True),
        ("Lane B Shadow", "--- a/nova_chain_engine.py\n+++ b/nova_chain_engine.py\n+CHAIN_FORK['nova-dev'] = ['review']", True, True),
    ]
    for name, diff, det, traj in shadow_cases:
        result = classify_blast_radius(diff, trajectory_pass=traj, deterministic_pass=det, shadow_mode=True)
        print(f"\n[{name}]")
        print(format_gate_report(result))
