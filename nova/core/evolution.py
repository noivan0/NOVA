"""
nova/core/evolution.py
----------------------
Evolution log — records every harness run outcome so NOVA can
learn from successes and failures over time.

Each entry is appended to <harness-dir>/evolution.md in human-readable
markdown, and also stored as JSON lines in evolution.jsonl for
programmatic analysis.

Entry schema (JSON):
{
  "run_id":        "run_20260425_100000_abc123",
  "harness":       "research",
  "pattern":       "pipeline",
  "started_at":    "ISO8601",
  "finished_at":   "ISO8601",
  "duration_secs": 272,
  "success":       true,
  "quality_score": 82,
  "phases_run":    ["slot", "keywords", "writer", "qa", "publisher"],
  "phases_failed": [],
  "runbook_fired": [],
  "improvements":  [],
  "notes":         ""
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


class EvolutionLog:
    def __init__(self, evolution_dir: str):
        """
        evolution_dir: directory where evolution.md and evolution.jsonl live.
        Usually the harness workspace directory.
        """
        self.dir = Path(evolution_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.md_path = self.dir / "evolution.md"
        self.jsonl_path = self.dir / "evolution.jsonl"

    def record(
        self,
        run_id: str,
        harness: str,
        pattern: str,
        started_at: str,
        success: bool,
        duration_secs: float,
        quality_score: Optional[int] = None,
        phases_run: Optional[List[str]] = None,
        phases_failed: Optional[List[str]] = None,
        runbook_fired: Optional[List[str]] = None,
        improvements: Optional[List[str]] = None,
        notes: str = "",
    ) -> None:
        finished_at = _now()

        entry = {
            "run_id": run_id,
            "harness": harness,
            "pattern": pattern,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_secs": round(duration_secs, 1),
            "success": success,
            "quality_score": quality_score,
            "phases_run": phases_run or [],
            "phases_failed": phases_failed or [],
            "runbook_fired": runbook_fired or [],
            "improvements": improvements or [],
            "notes": notes,
        }

        # Append JSON line
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        # Append Markdown entry
        self._append_markdown(entry)

    def recent(self, n: int = 10) -> List[dict]:
        """Return the N most recent evolution entries."""
        if not self.jsonl_path.exists():
            return []
        lines = self.jsonl_path.read_text().strip().splitlines()
        entries = [json.loads(l) for l in lines if l.strip()]
        return entries[-n:]

    def failure_rate(self, last_n: int = 10) -> float:
        """Return failure rate over the last N runs (0.0 - 1.0)."""
        entries = self.recent(last_n)
        if not entries:
            return 0.0
        failed = sum(1 for e in entries if not e["success"])
        return failed / len(entries)

    def consecutive_failures(self, symptom: Optional[str] = None) -> int:
        """Count how many of the most recent runs failed consecutively."""
        entries = self.recent(20)
        entries.reverse()  # newest first
        count = 0
        for e in entries:
            if not e["success"]:
                if symptom is None or symptom in e.get("notes", ""):
                    count += 1
                else:
                    break
            else:
                break
        return count

    def _append_markdown(self, e: dict) -> None:
        status = "SUCCESS" if e["success"] else "FAILURE"
        score_line = f"- quality_score: {e['quality_score']}\n" if e["quality_score"] is not None else ""
        failed_line = f"- phases_failed: {', '.join(e['phases_failed'])}\n" if e["phases_failed"] else ""
        runbook_line = f"- runbook_fired: {', '.join(e['runbook_fired'])}\n" if e["runbook_fired"] else ""
        improvement_line = (
            "- improvements:\n" + "".join(f"  - {i}\n" for i in e["improvements"])
            if e["improvements"] else ""
        )
        notes_line = f"- notes: {e['notes']}\n" if e["notes"] else ""

        block = (
            f"\n## {e['finished_at'][:10]} — {e['run_id']} [{status}]\n"
            f"- harness: {e['harness']} ({e['pattern']})\n"
            f"- duration: {e['duration_secs']}s\n"
            f"- phases_run: {', '.join(e['phases_run'])}\n"
            f"{score_line}{failed_line}{runbook_line}{improvement_line}{notes_line}"
        )

        with open(self.md_path, "a") as f:
            if not self.md_path.exists() or self.md_path.stat().st_size == 0:
                f.write(f"# Evolution Log — {e['harness']}\n")
            f.write(block)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
