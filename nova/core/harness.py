"""
nova/core/harness.py
--------------------
Loads and validates harness.yaml definitions.

Harness YAML structure:
  name: string
  description: string
  version: string
  pattern: pipeline | fanout | supervisor | generative
  persona: (optional) target user description
  phases:
    - id: string
      name: string
      description: string
      executor: llm | shell | python | passthrough
      prompt: string (for llm executor)
      command: string (for shell executor)
      input_files: list[str]   (from workspace/)
      output_file: str         (to workspace/)
      timeout: int             (override global)
      retries: int             (override global)
      quality_check: bool
      on_failure: skip | retry | abort | runbook
  runbook:                     (optional auto-recovery rules)
    - symptom: string
      action: string           (shell command or built-in keyword)
      escalate_after: int      (seconds, then notify)
  evolution:
    enabled: bool
    file: string               (default: evolution.md)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class PhaseDefinition:
    id: str
    name: str
    description: str = ""
    executor: str = "llm"             # llm | shell | python | passthrough
    prompt: str = ""
    command: str = ""
    input_files: List[str] = field(default_factory=list)
    output_file: str = ""
    timeout: Optional[int] = None
    retries: Optional[int] = None
    quality_check: bool = False
    on_failure: str = "retry"         # skip | retry | abort | runbook
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunbookRule:
    symptom: str
    action: str
    escalate_after: int = 3600        # seconds before notifying


@dataclass
class EvolutionConfig:
    enabled: bool = True
    file: str = "evolution.md"


@dataclass
class HarnessDefinition:
    name: str
    description: str = ""
    version: str = "1.0.0"
    pattern: str = "pipeline"         # pipeline | fanout | supervisor | generative
    persona: str = ""
    phases: List[PhaseDefinition] = field(default_factory=list)
    runbook: List[RunbookRule] = field(default_factory=list)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    metadata: Dict[str, Any] = field(default_factory=dict)


class HarnessLoader:
    """
    Discovers and loads harness definitions from the harnesses directory.

    Directory layout:
      harnesses/
        my-harness/
          harness.yaml        <- required
          prompts/            <- optional prompt templates
          agents/             <- optional per-phase agent definitions
          skills/             <- optional skill definitions
    """

    def __init__(self, harnesses_dir: str = "./harnesses"):
        self.harnesses_dir = Path(harnesses_dir)

    def list_harnesses(self) -> List[str]:
        """Return names of all discovered harnesses."""
        if not self.harnesses_dir.exists():
            return []
        return [
            d.name
            for d in sorted(self.harnesses_dir.iterdir())
            if d.is_dir() and (d / "harness.yaml").exists()
        ]

    def load(self, name: str) -> HarnessDefinition:
        """Load a harness by name."""
        path = self.harnesses_dir / name / "harness.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Harness '{name}' not found at {path}. "
                f"Available: {self.list_harnesses()}"
            )
        with open(path) as f:
            raw = yaml.safe_load(f)
        return _parse_harness(raw, base_dir=self.harnesses_dir / name)

    def load_from_file(self, path: str) -> HarnessDefinition:
        """Load a harness directly from a YAML file path."""
        p = Path(path)
        with open(p) as f:
            raw = yaml.safe_load(f)
        return _parse_harness(raw, base_dir=p.parent)


def _parse_harness(raw: dict, base_dir: Path) -> HarnessDefinition:
    if "name" not in raw:
        raise ValueError("harness.yaml must have a 'name' field")

    phases = []
    for ph in raw.get("phases", []):
        # Resolve prompt from external file if specified
        prompt = ph.get("prompt", "")
        if ph.get("prompt_file"):
            pf = base_dir / "prompts" / ph["prompt_file"]
            if pf.exists():
                prompt = pf.read_text()

        phases.append(PhaseDefinition(
            id=ph["id"],
            name=ph.get("name", ph["id"]),
            description=ph.get("description", ""),
            executor=ph.get("executor", "llm"),
            prompt=prompt,
            command=ph.get("command", ""),
            input_files=ph.get("input_files", []),
            output_file=ph.get("output_file", ""),
            timeout=ph.get("timeout"),
            retries=ph.get("retries"),
            quality_check=ph.get("quality_check", False),
            on_failure=ph.get("on_failure", "retry"),
            metadata=ph.get("metadata", {}),
        ))

    runbook = [
        RunbookRule(
            symptom=r["symptom"],
            action=r["action"],
            escalate_after=r.get("escalate_after", 3600),
        )
        for r in raw.get("runbook", [])
    ]

    evo_raw = raw.get("evolution", {})
    evolution = EvolutionConfig(
        enabled=evo_raw.get("enabled", True),
        file=evo_raw.get("file", "evolution.md"),
    )

    return HarnessDefinition(
        name=raw["name"],
        description=raw.get("description", ""),
        version=raw.get("version", "1.0.0"),
        pattern=raw.get("pattern", "pipeline"),
        persona=raw.get("persona", ""),
        phases=phases,
        runbook=runbook,
        evolution=evolution,
        metadata=raw.get("metadata", {}),
    )
