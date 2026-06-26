#!/usr/bin/env python3
"""
nova_search.py — NOVA 통합 검색 단일 진입점
nova_brain.db(벡터+BM25) + kb_unified_search(세션+스킬) 결합
RRF(Reciprocal Rank Fusion)로 최종 랭킹 통합

사용:
  python3 nova_search.py "검색어" [--top-k N] [--mode vector|keyword|hybrid]
  from nova_search import search  # 모듈로 import
"""
import os
import sys
import subprocess
import json
import re
from pathlib import Path

NOVA_BRAIN_CLI = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin/nova_brain_cli.py"
KB_UNIFIED     = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin/kb_unified_search.py"

# RRF 상수
RRF_K = 60
NOVA_SCORE_BOOST = 0.1    # nova_brain 벡터 원점수 부스트 가중치
EMOTIONAL_BOOST  = 0.05   # emotional_weight 부스트 가중치


def rrf_score(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def search(query: str, top_k: int = 10, mode: str = "hybrid") -> list:
    """
    통합 검색:
      mode=vector  : nova_brain 벡터 검색만
      mode=keyword : kb_unified BM25만
      mode=hybrid  : RRF 결합 (기본값)
    """
    results_nova  = []
    results_kb    = []

    # 1. nova_brain 벡터+BM25 검색
    if mode in ("hybrid", "vector"):
        try:
            r = subprocess.run(
                [sys.executable, str(NOVA_BRAIN_CLI), "search", query, "--top-k", str(top_k * 2)],
                capture_output=True, text=True, timeout=30
            )
            for line in r.stdout.split("\n"):
                m = re.match(r"(\d+)\.\s+\[([0-9.]+)\]\s+(.+?)\s+\((.+?)\)", line)
                if m:
                    results_nova.append({
                        "rank": int(m.group(1)),
                        "score": float(m.group(2)),
                        "title": m.group(3).strip(),
                        "section": m.group(4),
                        "source": "nova_brain",
                    })
                # path 줄
                elif results_nova and line.startswith("   ") and "/" in line and not line.startswith("    "):
                    results_nova[-1]["path"] = line.strip()
                # content 줄
                elif results_nova and line.startswith("   ") and "..." in line:
                    results_nova[-1]["content"] = line.strip()
        except Exception as e:
            print(f"[nova_brain search error: {e}]", file=sys.stderr)

    # 2. kb_unified_search (세션+KB+스킬)
    if mode in ("hybrid", "keyword"):
        try:
            r = subprocess.run(
                [sys.executable, str(KB_UNIFIED), query],
                capture_output=True, text=True, timeout=30
            )
            for line in r.stdout.split("\n"):
                m = re.match(r"\s+(\d+)\.\s+\[(\w+)\s+\]\s+(.+?)$", line)
                if m:
                    results_kb.append({
                        "rank": int(m.group(1)),
                        "score_raw": 0.0,
                        "title": m.group(3).strip(),
                        "source_type": m.group(2),
                        "source": "kb_unified",
                        "path": "",
                        "content": "",
                    })
                elif results_kb and "score=" in line:
                    m2 = re.search(r"score=([\d.]+)", line)
                    if m2:
                        results_kb[-1]["score_raw"] = float(m2.group(1))
        except Exception as e:
            print(f"[kb_unified error: {e}]", file=sys.stderr)

    if mode == "vector":
        return results_nova[:top_k]
    if mode == "keyword":
        # keyword 모드: score_raw -> rrf_score 로 노출 (표시 일관성)
        for r in results_kb:
            r["rrf_score"] = round(r.get("score_raw", 0.0), 4)
        return results_kb[:top_k]

    # 3. RRF 통합 (hybrid)
    combined = {}  # key: title → 통합 점수

    for rank, r in enumerate(results_nova):
        key = r.get("path") or r.get("title", "")
        if key not in combined:
            combined[key] = {"data": r, "rrf": 0.0}
        combined[key]["rrf"] += rrf_score(rank + 1)
        # nova_brain 원점수 부스트 (벡터 검색 신뢰도)
        combined[key]["rrf"] += r.get("score", 0) * NOVA_SCORE_BOOST
        # emotional_weight 부스트 (있으면)
        if r.get("emotional_weight"):
            combined[key]["rrf"] += float(r["emotional_weight"]) * EMOTIONAL_BOOST

    for rank, r in enumerate(results_kb):
        key = r.get("path") or r.get("title", "")
        if key not in combined:
            combined[key] = {"data": r, "rrf": 0.0}
        combined[key]["rrf"] += rrf_score(rank + 1)

    sorted_results = sorted(combined.values(), key=lambda x: x["rrf"], reverse=True)
    final = []
    for item in sorted_results[:top_k]:
        d = item["data"].copy()
        d["rrf_score"] = round(item["rrf"], 4)
        final.append(d)

    return final


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="nova_search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--mode", choices=["hybrid","vector","keyword"], default="hybrid")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    q = " ".join(args.query)
    results = search(q, top_k=args.top_k, mode=args.mode)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"=== NOVA 통합 검색: '{q}' (mode={args.mode}) ===\n")
        for i, r in enumerate(results, 1):
            score_str = f"rrf={r.get('rrf_score', r.get('score', 0)):.4f}"
            src = r.get("source", "")
            title = r.get("title", "")
            path = r.get("path", "")
            content = r.get("content", "")[:100]
            print(f"{i}. [{score_str}] {title} [{src}]")
            if path:
                print(f"   {path}")
            if content:
                print(f"   {content}...")
            print()
