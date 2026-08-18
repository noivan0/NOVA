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


def _packaged_harnesses_dir() -> Optional[Path]:
    """Return the harnesses/ directory bundled inside the installed nova package.

    P0 install fix: example harnesses (research, summarizer, data-pipeline, ...)
    now ship as package data under ``nova/harnesses/`` (see pyproject.toml
    package-data + MANIFEST.in). A fresh ``pip install nova-orchestrator`` does
    NOT create a ``./harnesses`` directory in the user's current working
    directory — only ``HarnessLoader`` falling back to the packaged copy makes
    ``nova list`` / ``nova run <harness>`` work immediately after install with
    no git clone and no extra setup step.
    """
    try:
        import importlib.resources as ir

        resource = ir.files("nova") / "harnesses"
        if resource.is_dir():
            return Path(str(resource))
    except Exception:
        pass
    return None


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
    # per-phase LLM 오버라이드 (미지정 시 nova.yaml 기본 LLM 사용)
    provider: Optional[str] = None    # anthropic | openai | codex | hmg_gemini
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
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
        # P0 install fix: if the configured/cwd harnesses dir doesn't exist or is
        # empty (typical right after `pip install` with no git clone), fall back
        # to the example harnesses bundled inside the installed package so
        # `nova list` / `nova run <name>` work with zero extra setup.
        self._fallback_dir: Optional[Path] = None
        if not self._dir_has_harnesses(self.harnesses_dir):
            packaged = _packaged_harnesses_dir()
            if packaged is not None and self._dir_has_harnesses(packaged):
                self._fallback_dir = packaged

    @staticmethod
    def _dir_has_harnesses(d: Path) -> bool:
        if not d.exists():
            return False
        return any(
            sub.is_dir() and (sub / "harness.yaml").exists() for sub in d.iterdir()
        )

    def _active_dir(self) -> Path:
        return self._fallback_dir if self._fallback_dir is not None else self.harnesses_dir

    def list_harnesses(self) -> List[str]:
        """Return names of all discovered harnesses."""
        active = self._active_dir()
        if not active.exists():
            return []
        return [
            d.name
            for d in sorted(active.iterdir())
            if d.is_dir() and (d / "harness.yaml").exists()
        ]

    def load(self, name: str) -> HarnessDefinition:
        """Load a harness by name."""
        active = self._active_dir()
        path = active / name / "harness.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Harness '{name}' not found at {path}. "
                f"Available: {self.list_harnesses()}"
            )
        with open(path) as f:
            raw = yaml.safe_load(f)
        return _parse_harness(raw, base_dir=active / name)

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
            else:
                import warnings
                warnings.warn(
                    f"[nova] prompt_file '{ph['prompt_file']}' not found at {pf} "
                    f"for phase '{ph.get('id', '?')}'. Phase will use an empty prompt.",
                    stacklevel=2,
                )

        phases.append(PhaseDefinition(
            id=ph["id"],
            name=ph.get("name", ph["id"]),
            description=ph.get("description", ""),
            executor=ph.get("executor", "llm"),
            prompt=prompt,
            command=ph.get("command", ph.get("script", "")),  # BUG-HARNESS: script 필드 폴백
            input_files=ph.get("input_files", []),
            output_file=ph.get("output_file", ""),
            timeout=ph.get("timeout"),
            retries=ph.get("retries"),
            quality_check=ph.get("quality_check", False),
            on_failure=ph.get("on_failure", "retry"),
            # per-phase LLM 오버라이드
            provider=ph.get("provider"),
            model=ph.get("model"),
            api_key=ph.get("api_key"),
            base_url=ph.get("base_url"),
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
