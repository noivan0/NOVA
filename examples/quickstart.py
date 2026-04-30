"""
examples/quickstart.py
-----------------------
Minimal programmatic usage of NOVA.
Runs a single-phase harness entirely in Python — no CLI, no harness.yaml file.

Usage:
    NOVA_LLM_API_KEY=sk-... python examples/quickstart.py
"""

from pathlib import Path
from nova.core.config import load_config
from nova.core.harness import HarnessDefinition, PhaseDefinition
from nova.core.orchestrator import Orchestrator

# 1. Load config from nova.yaml + environment variables
config = load_config("nova.yaml")

# 2. Define a harness programmatically (or load from a YAML file)
harness = HarnessDefinition(
    name="quickstart",
    description="Write a haiku about AI",
    version="1.0.0",
    pattern="pipeline",
    phases=[
        PhaseDefinition(
            id="write",
            name="Write Haiku",
            executor="llm",
            prompt="Write a haiku about artificial intelligence. Just the haiku, no explanation.",
            output_file="haiku.txt",
            on_failure="abort",
        )
    ],
)

# 3. Run the harness
orchestrator = Orchestrator(config)
context = {"title": "AI Haiku"}
result = orchestrator.run(harness, context=context)

# 4. Read the output
workspace = Path(config.workspace) / "quickstart"
haiku = (workspace / "haiku.txt").read_text()
print("Generated haiku:")
print(haiku)
