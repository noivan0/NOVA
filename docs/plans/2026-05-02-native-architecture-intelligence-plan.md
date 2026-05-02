# NOVA-oss Native Architecture Intelligence Plan

## Goal
Build a native Graphify-like architecture intelligence capability inside NOVA-oss without importing Graphify itself, then validate it on NOVA-oss as an independent proving ground.

## Strategic constraints
- Must be native to NOVA-oss.
- Must focus on the valuable signals discovered in the Graphify pilot, not full tool parity.
- Must keep NOVA and NOVA-oss implementation tracks independent.
- Must produce machine-readable output plus human-readable report.
- Must support later independent re-implementation/port into NOVA.

## Phase 1 scope
Deliver a conservative, code-centric analyzer for Python repos that provides:
1. Structural index build
2. Centrality hotspot report
3. Bridge-node approximation
4. Path tracing between symbols/files
5. KB/report-friendly markdown output

Non-goals for Phase 1:
- fuzzy explain/query UX
- semantic doc-to-code linking
- cross-language support
- inferred edges from LLM guesses
- deep IDE integration

## Native command surface
Add a new CLI group:
- `nova inspect build [repo]`
- `nova inspect report [repo]`
- `nova inspect path [repo] --from X --to Y`
- `nova inspect hotspots [repo]`
- `nova inspect bridges [repo]`

Defaults:
- repo defaults to current working directory
- output defaults to `<repo>/.nova-arch/`

## Data model
Nodes:
- module
- class
- function

Edges:
- contains (module->class, module->function, class->function)
- imports (module->module-ish reference)
- calls (function/class method -> called symbol name)
- inherits (class -> base class symbol)

Derived signals:
- in_degree
- out_degree
- total_degree
- bridge_score (count of unique neighboring modules crossing local module boundary)
- layer_guess (cli/core/provider/other based on path prefix)

## Files to add
- `nova/inspect/__init__.py`
- `nova/inspect/models.py`
- `nova/inspect/analyzer.py`
- `nova/inspect/report.py`

## Files to modify
- `nova/cli/main.py`
- `README.md`
- `tests/unit/test_architecture_inspector.py` (new)
- `tests/integration/test_architecture_cli.py` (new)

## Output artifacts
Inside `.nova-arch/`:
- `graph.json`
- `report.md`
- `summary.json`

## Validation plan on NOVA-oss
Success criteria:
1. Build completes on NOVA-oss repo
2. Detects key nodes such as Orchestrator, HarnessLoader, Checkpoint, EvolutionLog, KB, LLMConfig
3. Path query can recover `main -> Orchestrator` and `Orchestrator -> Checkpoint`
4. Report is stable and understandable
5. Test suite passes

## Portability to NOVA
Keep code independent from repo-specific constants except path-based layer heuristics. Port by copying module with minimal surface changes, then validate on NOVA independently.
