"""
nova/core/checkpoint.py
-----------------------
Checkpoint system for resumable phase execution.

On each phase transition, the current state is written to
workspace/checkpoint.json. If NOVA is restarted, it reads
this file and resumes from the last completed phase.

Schema:
{
  "harness":        "harness-name",
  "run_id":         "run_YYYYMMDD_HHMMSS",
  "phase":          3,
  "phase_id":       "quality_checker",
  "state":          { ...arbitrary phase state... },
  "started_at":     "ISO8601",
  "phase_started_at": "ISO8601",
  "stale_threshold_secs": 300
}
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class Checkpoint:
    FILENAME = "checkpoint.json"

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._path = self.workspace / self.FILENAME

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self, harness: str, stale_threshold_secs: int = 300) -> str:
        """Create a new run checkpoint. Returns run_id."""
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self._write({
            "harness": harness,
            "run_id": run_id,
            "phase": 0,
            "phase_id": None,
            "state": {},
            "started_at": _now(),
            "phase_started_at": _now(),
            "stale_threshold_secs": stale_threshold_secs,
        })
        return run_id

    def update(self, phase_index: int, phase_id: str, state: Dict[str, Any]) -> None:
        """Advance checkpoint to a new phase."""
        data = self._read() or {}
        data.update({
            "phase": phase_index,
            "phase_id": phase_id,
            "state": state,
            "phase_started_at": _now(),
        })
        self._write(data)

    def complete(self) -> None:
        """Mark the run as done and remove the checkpoint file."""
        if self._path.exists():
            self._path.unlink()

    def resume(self) -> Optional[Dict[str, Any]]:
        """
        Return the saved checkpoint if one exists, otherwise None.
        Also checks if the checkpoint is stale (phase started too long ago).
        """
        data = self._read()
        if data is None:
            return None

        threshold = data.get("stale_threshold_secs", 300)
        phase_started = data.get("phase_started_at")
        if phase_started:
            delta = (datetime.now(timezone.utc) - _parse_iso(phase_started)).total_seconds()
            if delta > threshold:
                print(
                    f"[checkpoint] STALE: phase '{data.get('phase_id')}' "
                    f"started {int(delta)}s ago (threshold={threshold}s). "
                    f"Clearing checkpoint and restarting."
                )
                self.complete()
                return None

        return data

    def exists(self) -> bool:
        return self._path.exists()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _write(self, data: dict) -> None:
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    def _read(self) -> Optional[dict]:
        if not self._path.exists():
            return None
        with open(self._path) as f:
            return json.load(f)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)
