#!/usr/bin/env python3
"""
nova_dream.py — NOVA Dream Cycle (GBrain 드림 사이클 이식)

GBrain 원본 14개 페이즈 → NOVA 8개 페이즈로 압축:

Phase 1: sync          변경된 KB 파일 nova_brain 인덱싱
Phase 2: synthesize    최근 대화 → CT+TL KB 자동 변환
Phase 3: extract       KB 내 링크/참조 자동 추출 → trajectories
Phase 4: patterns      크로스페이지 반복 테마 감지 → pattern 페이지
Phase 5: consolidate   중복/유사 Takes 통합
Phase 6: contradictions 모순 감지 + LLM 판정
Phase 7: embed         미처리 청크 벡터화
Phase 8: health        헬스 점수 측정 + 임계값 경고

사용:
  python3 nova_dream.py               # 전체 사이클
  python3 nova_dream.py --phase sync  # 특정 페이즈만
  python3 nova_dream.py --dry-run     # 시뮬레이션
"""
import sys
import os
import re
import json
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "bin"))
from nova_llm import call_llm as call_haiku

KB_ROOT   = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
NOVA_BIN  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin"
LOG_PATH  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "logs/nova_dream.log"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def run(cmd: list, timeout: int = 120) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERROR: {e}]"


def get_brain():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nova_brain", str(NOVA_BIN / "nova_brain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NovaBrain()


# ── Phase 1: sync ─────────────────────────────────────
def phase_sync(dry_run: bool = False) -> dict:
    log("[Phase 1] sync — 변경된 KB 파일 인덱싱")
    if dry_run:
        return {"phase": "sync", "status": "dry-run"}
    out = run([sys.executable, str(NOVA_BIN / "nova_kb_sync.py")])
    m = re.search(r"추가=(\d+).*업데이트=(\d+).*스킵=(\d+)", out)
    result = {"phase": "sync", "added": 0, "updated": 0, "skipped": 0}
    if m:
        result.update({"added": int(m.group(1)),
                       "updated": int(m.group(2)),
                       "skipped": int(m.group(3))})
    log(f"  sync: 추가={result['added']}, 업데이트={result['updated']}")
    return result


# ── Phase 2: synthesize ───────────────────────────────
def phase_synthesize(dry_run: bool = False) -> dict:
    log("[Phase 2] synthesize — 최근 대화 → CT+TL KB 변환")
    if dry_run:
        return {"phase": "synthesize", "status": "dry-run"}
    out = run([sys.executable,
               str(NOVA_BIN / "nova_brain_synthesize.py"),
               "--auto", "--days", "1"], timeout=300)
    m = re.search(r"synthesize 완료: (\d+)개", out)
    count = int(m.group(1)) if m else 0
    log(f"  synthesize: {count}개 새 KB 항목")
    return {"phase": "synthesize", "new_pages": count}


# ── Phase 3: extract ──────────────────────────────────
def phase_extract(dry_run: bool = False) -> dict:
    """KB 파일에서 수치 메트릭 자동 추출 → trajectories"""
    log("[Phase 3] extract — 수치 메트릭 자동 추출")
    if dry_run:
        return {"phase": "extract", "status": "dry-run"}

    brain = get_brain()
    extracted = 0

    # 에이전트 KB 파일에서 메트릭 패턴 추출
    metric_patterns = [
        (r"_run_.*?:\s*(\d+)", "run_methods"),
        (r"P&L.*?([+-]?\d+\.?\d*%)", "pnl"),
        (r"승률.*?(\d+\.?\d*%)", "win_rate"),
        (r"KB\s+파일.*?(\d+)개", "kb_files"),
        (r"벡터.*?(\d+)건", "vectors"),
        (r"health.*?(\d+\.?\d*)/100", "health_score"),
    ]

    today = datetime.now().strftime("%Y-%m-%d")
    # 확장 스캔: kb/agents + wiki/
    HERMES_HOME = KB_ROOT.parent
    scan_dirs = [
        (KB_ROOT / "agents", KB_ROOT),    # 기존 에이전트 KB
        (HERMES_HOME / "wiki", HERMES_HOME),  # wiki 추가
    ]
    for scan_dir, base_dir in scan_dirs:
        if not scan_dir.exists(): continue
        for f in scan_dir.rglob("*.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                rel = str(f.relative_to(base_dir))
                for pattern, metric in metric_patterns:
                    m = re.search(pattern, text, re.IGNORECASE)
                    if m:
                        raw = m.group(1).replace("%","").replace("+","")
                        try:
                            val = float(raw)
                            brain.record_metric(rel, metric, val, period=today)
                            extracted += 1
                        except ValueError:
                            pass
            except Exception:
                continue

    brain.close()
    log(f"  extract: {extracted}개 메트릭 기록")
    return {"phase": "extract", "metrics": extracted}


# ── Phase 4: patterns ─────────────────────────────────
def phase_patterns(dry_run: bool = False) -> dict:
    """크로스페이지 반복 테마 감지 → pattern 페이지 생성"""
    log("[Phase 4] patterns — 반복 테마 감지")
    if dry_run:
        return {"phase": "patterns", "status": "dry-run"}

    brain = get_brain()

    # 최근 업데이트된 페이지들에서 키워드 빈도 분석
    pages = brain.conn.execute("""
        SELECT path, compiled_truth FROM pages
        WHERE page_type IN ('project','fix','agent') AND compiled_truth IS NOT NULL
        AND length(compiled_truth) > 200
        ORDER BY updated_at DESC LIMIT 100
    """).fetchall()

    # 공통 테마 키워드 집계
    import collections
    keyword_pages = collections.defaultdict(list)
    important_terms = set()

    for path, ct in pages:
        words = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b|\b[가-힣]{3,}\b', ct or "")
        for w in words:
            if len(w) > 3:
                keyword_pages[w].append(path)

    # 3개 이상 페이지에 등장하는 테마
    patterns_found = []
    for kw, plist in keyword_pages.items():
        if len(set(plist)) >= 3:
            patterns_found.append((kw, list(set(plist))[:5]))

    patterns_found.sort(key=lambda x: len(x[1]), reverse=True)
    brain.close()

    # 상위 패턴이 있으면 패턴 페이지 생성
    created = 0
    if patterns_found[:5]:
        today = datetime.now().strftime("%Y-%m-%d")
        pattern_path = KB_ROOT / "agents" / "nova-evaluator" / f"nova-patterns-{today}.md"
        lines = [
            "---",
            "title: NOVA 크로스페이지 패턴",
            "page_type: pattern",
            "agent: nova-evaluator",
            f"date: {today}",
            "---",
            "",
            "## Compiled Truth",
            "",
            f"# 반복 테마 감지 ({today})",
            "",
            "## 감지된 패턴\n",
        ]
        for kw, plist in patterns_found[:10]:
            lines.append(f"### {kw}")
            lines.append(f"등장: {len(plist)}개 페이지")
            for p in plist[:3]:
                lines.append(f"- {p}")
            lines.append("")
        lines += [
            "## Timeline",
            "",
            "> 추가전용",
            "",
            f"- {today}: [nova_dream] patterns 페이즈 — {len(patterns_found)}개 테마 감지",
        ]
        pattern_path.write_text("\n".join(lines), encoding="utf-8")
        created = 1
        log(f"  patterns: {len(patterns_found)}개 테마 감지, 패턴 페이지 생성")

    return {"phase": "patterns", "themes": len(patterns_found), "pages_created": created}


# ── Phase 5: consolidate ──────────────────────────────
def phase_consolidate(dry_run: bool = False) -> dict:
    """중복 Takes 통합
    BUG-DREAM-C1 fix: ORDER BY weight DESC, created_at DESC
    → 더 높은 weight의 take가 "원본"으로 살아남아야 함
    이전: ORDER BY created_at ASC (오래된 것이 원본 = 72% hq takes 제거 버그)
    수정: weight 높은 것 먼저 처리 → seen_claims에 먼저 등록 → 최고품질 take 보존
    """
    log("[Phase 5] consolidate — 중복 Takes 통합")
    if dry_run:
        return {"phase": "consolidate", "status": "dry-run"}

    brain = get_brain()
    takes = brain.conn.execute("""
        SELECT id, holder, claim, weight FROM takes
        WHERE superseded_by IS NULL
        ORDER BY weight DESC, created_at DESC
    """).fetchall()

    consolidated = 0
    seen_claims = {}
    now = datetime.now(timezone.utc).isoformat()

    for take_id, holder, claim, weight in takes:
        key = claim[:50].lower().strip()
        if key in seen_claims:
            # 중복 — supersede (현재 take이 더 낮은 weight이므로 supersede됨)
            brain.conn.execute(
                "UPDATE takes SET superseded_by=?, updated_at=? WHERE id=?",
                (seen_claims[key], now, take_id)
            )
            consolidated += 1
        else:
            seen_claims[key] = take_id

    brain.conn.commit()
    brain.close()
    log(f"  consolidate: {consolidated}개 중복 Takes 통합 (weight 우선 보존)")
    return {"phase": "consolidate", "consolidated": consolidated}


# ── Phase 5.5: calibrate ──────────────────────────────
def phase_calibrate(dry_run: bool = False) -> dict:
    """bet Takes Brier Score 계산 및 brain_health calibration_score 기록"""
    log("[Phase 5.5] calibrate — bet Takes Brier Score 계산")
    if dry_run:
        return {"phase": "calibrate", "status": "dry-run"}

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "nova_calibration", str(NOVA_BIN / "nova_calibration.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        result = mod.compute_calibration()  # nova_calibration.py API 사용
        n_agents = len(result)
        total_bets = sum(d.get("n_bets", 0) for d in result.values())
        log(f"  calibrate: {total_bets}개 bet 판정, 에이전트={n_agents}")
        return {
            "phase": "calibrate",
            "total_resolved_bets": total_bets,
            "n_agents": n_agents,
        }
    except Exception as e:
        log(f"  calibrate 오류: {e}", )
        return {"phase": "calibrate", "error": str(e)}


# ── Phase 5.6: emotional_weight ────────────────────────
def phase_emotional_weight(dry_run: bool = False) -> dict:
    """전체 페이지 감정가중치 재계산"""
    log("[Phase 5.6] emotional_weight — 전체 페이지 감정가중치 재계산")
    if dry_run:
        return {"phase": "emotional_weight", "status": "dry-run"}

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "nova_emotional", str(NOVA_BIN / "nova_emotional.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        updated = mod.compute_emotional_weights()
        log(f"  emotional_weight: {updated}개 페이지 갱신")
        return {"phase": "emotional_weight", "updated_pages": updated}
    except Exception as e:
        log(f"  emotional_weight 오류: {e}")
        return {"phase": "emotional_weight", "error": str(e)}


# ── Phase 5.7: on_done_takes ───────────────────────────
def phase_on_done_takes(dry_run: bool = False) -> dict:
    """Kanban done 태스크 → Takes 자동 등록 (nova_on_done_takes bulk_register)
    Round6 linkage: nova_on_done_takes.py를 Dream Cycle에 연결
    """
    log("[Phase 5.7] on_done_takes — Kanban done 태스크 → Takes 등록")
    if dry_run:
        return {"phase": "on_done_takes", "status": "dry-run"}

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "nova_on_done_takes", str(NOVA_BIN / "nova_on_done_takes.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok = mod.bulk_register()
        log(f"  on_done_takes: {ok}개 takes 등록")
        return {"phase": "on_done_takes", "registered": ok}
    except Exception as e:
        log(f"  on_done_takes 오류: {e}")
        return {"phase": "on_done_takes", "error": str(e)}


# ── Phase 6: contradictions ───────────────────────────
def phase_contradictions(dry_run: bool = False) -> dict:
    """모순 감지 (키워드 기반, LLM 판정은 주간 배치로) + low severity 자동 dismiss"""
    log("[Phase 6] contradictions — 모순 감지")
    if dry_run:
        return {"phase": "contradictions", "status": "dry-run"}

    brain = get_brain()
    found = brain.detect_contradictions(top_k_pairs=10)  # 빠른 실행

    # low severity 자동 dismiss (Codex HIGH 권고: 매 Dream Cycle마다 40개 누적 원인 차단)
    # claim_a[:80] == claim_b[:80] 인 완전 동일 문서 쌍 OR low severity 전체 auto-dismiss
    auto_dismissed = 0
    try:
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        # 방금 감지된 것 중 low severity: claim 앞부분 동일하거나 모두 low → dismiss
        for c in found:
            if c.get("severity") == "low":
                brain.conn.execute(
                    "UPDATE contradictions SET status='dismissed', resolution=?, resolved_at=? WHERE id=? AND status='open'",
                    ("auto:low_severity_phase6_dismiss — KB 간 정상 cross-reference", now_iso, c["id"])
                )
                auto_dismissed += 1
        # 누적된 기존 low severity open 항목도 일괄 처리 (이번 배치 외 과거 누적분)
        old_low = brain.conn.execute(
            "SELECT id FROM contradictions WHERE status='open' AND severity='low'"
        ).fetchall()
        for (cid,) in old_low:
            brain.conn.execute(
                "UPDATE contradictions SET status='dismissed', resolution=?, resolved_at=? WHERE id=?",
                ("auto:low_severity_bulk_dismiss — 자동화 정리", now_iso, cid)
            )
            auto_dismissed += 1
    except Exception as e:
        log(f"  [WARN] auto-dismiss 실패: {e}")

    brain.conn.commit()
    brain.close()
    log(f"  contradictions: {len(found)}개 감지 / {auto_dismissed}개 low severity 자동 dismiss")
    return {"phase": "contradictions", "detected": len(found), "auto_dismissed": auto_dismissed, "judged": 0}





# ── Phase 7: embed ────────────────────────────────────
def phase_embed(dry_run: bool = False) -> dict:
    """미처리 벡터 배치 생성"""
    log("[Phase 7] embed — 미처리 벡터 생성")
    if dry_run:
        return {"phase": "embed", "status": "dry-run"}

    brain = get_brain()
    pending = brain.conn.execute("""
        SELECT COUNT(*) FROM page_chunks pc
        LEFT JOIN chunk_vectors cv ON pc.id = cv.chunk_id
        WHERE cv.chunk_id IS NULL
    """).fetchone()[0]
    brain.close()

    if pending == 0:
        log("  embed: 미처리 없음")
        return {"phase": "embed", "embedded": 0}

    log(f"  embed: {pending}개 처리 중...")
    out = run([sys.executable,
               str(NOVA_BIN / "nova_brain_embed.py"), "--sync"],
              timeout=300)
    m = re.search(r"임베딩 완료: (\d+)개", out)
    count = int(m.group(1)) if m else 0
    log(f"  embed: {count}개 완료")
    return {"phase": "embed", "embedded": count}


# ── Phase 8: health ───────────────────────────────────
def phase_health(dry_run: bool = False) -> dict:
    """헬스 측정 + 임계값 경고 + 개별 페이지 health_score 실제 계산"""
    log("[Phase 8] health — 헬스 측정")
    if dry_run:
        return {"phase": "health", "status": "dry-run"}

    brain = get_brain()
    h = brain.measure_health()

    # [수정 2026-05-22] 개별 페이지 health_score 실제 계산 (기본값 1.0 고착 수정)
    # 스코어 = chunk_depth(0~0.35) + takes_bonus(0.25) + char_quality(0~0.25) + contradiction_penalty(-0.15) + base(0.15)
    try:
        updated = brain.conn.execute("""
            UPDATE pages SET health_score = (
                SELECT
                    MIN(1.0, MAX(0.05,
                        0.15
                        + MIN(0.35, CAST(COUNT(pc.id) AS REAL) / 10.0)
                        + CASE WHEN EXISTS(SELECT 1 FROM takes t WHERE t.page_id=pages.id AND t.superseded_by IS NULL) THEN 0.25 ELSE 0.0 END
                        + MIN(0.25, CAST(COALESCE(SUM(pc.char_count), 0) AS REAL) / 6000.0)
                        + CASE WHEN pages.has_contradictions=1 THEN -0.15 ELSE 0.0 END
                    ))
                FROM page_chunks pc WHERE pc.page_id=pages.id
            )
            WHERE id IN (SELECT id FROM pages)
        """).rowcount
        brain.conn.commit()
        log(f"  health_score 실제 계산 완료: {updated}개 페이지 업데이트")
    except Exception as e:
        log(f"  health_score 계산 실패(무시): {e}")

    brain.close()

    import json as _json
    alerts = _json.loads(h.get("thresholds_crossed") or "[]")
    log(f"  health: {h['score_overall']}/100, alerts={len(alerts)}")
    if alerts:
        for a in alerts:
            log(f"  ⚠ {a}")
    return {"phase": "health", **{k: v for k, v in h.items()
                                   if k != "thresholds_crossed"},
            "alerts": alerts}


# ── Dream Cycle 전체 ──────────────────────────────────
PHASES = {
    "sync":            phase_sync,
    "synthesize":      phase_synthesize,
    "extract":         phase_extract,
    "patterns":        phase_patterns,
    "consolidate":     phase_consolidate,
    "calibrate":       phase_calibrate,
    "emotional_weight": phase_emotional_weight,
    "on_done_takes":   phase_on_done_takes,  # Round6 linkage: Kanban done → Takes
    "contradictions":  phase_contradictions,
    "embed":           phase_embed,
    "health":          phase_health,
}

def run_dream(phases: list = None, dry_run: bool = False):
    start = time.time()
    log("=" * 50)
    log(f"NOVA Dream Cycle 시작 {'[DRY-RUN]' if dry_run else ''}")

    if phases is None:
        phases = list(PHASES.keys())

    results = []
    for phase_name in phases:
        if phase_name not in PHASES:
            log(f"[SKIP] 알 수 없는 페이즈: {phase_name}")
            continue
        try:
            t0 = time.time()
            result = PHASES[phase_name](dry_run=dry_run)
            result["elapsed_s"] = round(time.time() - t0, 1)
            results.append(result)
        except Exception as e:
            log(f"[ERROR] {phase_name}: {e}")
            results.append({"phase": phase_name, "error": str(e)})

    elapsed = round(time.time() - start, 1)
    log(f"Dream Cycle 완료: {elapsed}s")

    # 결과 저장
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = KB_ROOT / "agents" / "nova-evaluator" / f"{today}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        f"title: NOVA Dream Cycle {today}",
        "agent: nova-evaluator",
        "page_type: evaluation",
        f"date: {today}",
        "---",
        "",
        "## Compiled Truth",
        "",
        f"# NOVA Dream Cycle 실행 결과 ({today})",
        f"총 소요: {elapsed}초",
        "",
    ]
    for r in results:
        phase = r.get("phase","?")
        err   = r.get("error","")
        elapsed_p = r.get("elapsed_s","?")
        lines.append(f"## {phase} ({elapsed_p}s)")
        if err:
            lines.append(f"ERROR: {err}")
        else:
            for k, v in r.items():
                if k not in ("phase","elapsed_s"):
                    lines.append(f"- {k}: {v}")
        lines.append("")

    lines += [
        "## Timeline",
        "",
        "> 추가전용",
        "",
        f"- {today}: [nova_dream] Dream Cycle 실행 — {elapsed}s",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"보고서: {report_path}")

    # agent_activity 로깅 (Dream Cycle 실행 기록)
    if not dry_run:
        try:
            brain = get_brain()
            brain.log_activity(
                agent="nova-evaluator",
                action="dream_cycle",
                target_path=str(report_path.relative_to(KB_ROOT)),
                summary=f"Dream Cycle {elapsed}s — phases: {phases or list(PHASES.keys())}",
            )
            brain.close()
        except Exception:
            pass

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", nargs="+",
                        choices=list(PHASES.keys()),
                        help="실행할 페이즈 (기본: 전체)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dream(phases=args.phase, dry_run=args.dry_run)
