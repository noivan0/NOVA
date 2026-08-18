"""tests/unit/test_harness_loader_path_traversal.py — regression test for
HarnessLoader.load()'s missing name validation.

SECURITY-008 (2026-08-18, Codex-audited round 3): `load(name)` joined
`name` into `active / name / "harness.yaml"` with zero validation. Codex
reproduced `load("../outside-harness")` loading an attacker-planted
harness.yaml entirely outside the intended harnesses directory, whose
`output_file: ../../escaped-output.md` could then write outside the
workspace during a real run. `load(name)` is a name-based lookup by
design (unlike `load_from_file()`, which legitimately accepts an
arbitrary file path) — fixed by rejecting any name that resolves outside
the active harnesses directory.
"""
from pathlib import Path

import pytest

from nova.core.harness import HarnessLoader


@pytest.fixture
def active_dir(tmp_path: Path) -> Path:
    active = tmp_path / "harnesses"
    active.mkdir()
    legit = active / "legit"
    legit.mkdir()
    (legit / "harness.yaml").write_text("name: legit\nphases: []\n")
    return active


def test_normal_load_still_works(active_dir: Path):
    loader = HarnessLoader(str(active_dir))
    h = loader.load("legit")
    assert h.name == "legit"


def test_load_rejects_traversal_name(active_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside-harness"
    outside.mkdir()
    (outside / "harness.yaml").write_text(
        "name: outside\nphases:\n"
        "  - id: p\n    executor: shell\n    command: echo x\n"
        "    output_file: ../../escaped-output.md\n"
    )
    loader = HarnessLoader(str(active_dir))
    with pytest.raises(ValueError):
        loader.load("../outside-harness")


@pytest.mark.parametrize("evil_name", [
    "../outside-harness",
    "../../etc",
    "a/../../outside-harness",
])
def test_load_rejects_various_traversal_names(active_dir: Path, evil_name: str):
    loader = HarnessLoader(str(active_dir))
    with pytest.raises((ValueError, FileNotFoundError)):
        loader.load(evil_name)
