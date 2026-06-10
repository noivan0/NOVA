# NOVA Native Architecture Intelligence Port Plan

> For Hermes: implement this as an independent NOVA port, not by importing nova-oss package internals.

Goal:
Port the proven native architecture intelligence capability from NOVA-oss into NOVA itself, preserving repo independence while validating on the real Hermes runtime codebase.

Architecture:
Create a NOVA-native inspection module with the same conceptual surface as NOVA-oss (`build`, `report`, `hotspots`, `bridges`, `path`) but keep implementation copied/adapted into NOVA directly. Re-validate on the actual NOVA repository and tune layer heuristics for Hermes runtime paths.

Tech stack:
- Python stdlib only for analysis core (`ast`, `json`, `collections`, `pathlib`)
- Existing NOVA CLI architecture
- Markdown + JSON artifact output

---

## Why this next

NOVA-oss is now a validated proving ground. The highest-value next step is porting into NOVA because:
1. it turns the experiment into value on the real system
2. it validates the design against the more complex Hermes runtime
3. it creates the base for later inspect/report/diagnostic workflows inside NOVA itself

## Scope

Phase 1 port should include:
- native inspect package in NOVA
- CLI command surface equivalent to NOVA-oss
- graph artifact output
- hotspot/bridge/path/report queries
- repo-specific layer heuristics for Hermes/NOVA layout
- smoke validation on real NOVA codebase

Non-goals for this port step:
- semantic search / LLM explain mode
- cross-language parsing
- incremental index caching
- CI redesign
- Graphify dependency integration

## Source reference

Use these as conceptual references only:
- $NOVA_HOME/nova/inspect/__init__.py
- $NOVA_HOME/nova/inspect/analyzer.py
- $NOVA_HOME/nova/inspect/models.py
- $NOVA_HOME/nova/inspect/report.py
- $NOVA_HOME/nova/cli/main.py

Do not create a runtime dependency from NOVA to NOVA-oss.

## Target workspace

Primary candidate:
- $NOVA_HOME

If porting should instead land in another NOVA runtime tree, discover that path first before coding.

## Proposed files in NOVA

Create:
- agent/architecture_intel/__init__.py
- agent/architecture_intel/analyzer.py
- agent/architecture_intel/models.py
- agent/architecture_intel/report.py
- tests/unit/test_architecture_intel.py
- tests/integration/test_architecture_cli.py or nearest equivalent CLI integration test file

Modify:
- hermes_cli/main.py or the actual CLI command registration file used by the current runtime
- relevant help/docs surface if command registry requires it

## Porting adaptations required

1. Layer heuristics
NOVA-oss used:
- cli
- core
- provider
- test
- other

Hermes/NOVA runtime should likely infer layers such as:
- cli
- agent
- tools
- gateway
- tests
- platform
- runtime
- other

2. Directory skips
Expand skip set to include runtime-specific generated/cache dirs if present.

3. Symbol/path examples for validation
Use real Hermes/NOVA paths such as:
- main/CLI entry -> AIAgent
- AIAgent -> model_tools
- AIAgent -> terminal tool path
- gateway/run -> platform adapter

## Validation tasks

### Task 1: Discover the exact NOVA target tree
Objective:
Confirm the repository where the port should land.

Files:
- Inspect only

Steps:
1. Verify candidate runtime repo path.
2. Verify CLI entry file and existing test layout.
3. Identify exact command registration file.

Verification:
- confirmed absolute repo path
- confirmed file list for implementation

### Task 2: Create NOVA-native data models
Objective:
Port graph dataclasses without external dependencies.

Files:
- Create: agent/architecture_intel/models.py
- Create: agent/architecture_intel/__init__.py

Steps:
1. Copy/adapt node, edge, graph, path result dataclasses.
2. Ensure JSON serialization helpers are self-contained.
3. Export clean public surface from __init__.py.

Verification:
- import module successfully
- simple instantiate/serialize roundtrip works

### Task 3: Port analyzer core
Objective:
Implement Python structural graph builder in NOVA.

Files:
- Create: agent/architecture_intel/analyzer.py

Steps:
1. Port file discovery and AST parsing.
2. Port node/edge extraction.
3. Add Hermes/NOVA-specific layer guessing.
4. Keep `.hermes-arch/` or equivalent output dir local to analyzed repo.
5. Implement save/load/find_path/top_hotspots/top_bridges.

Verification:
- analyzer builds graph on target repo
- graph JSON emits successfully

### Task 4: Port report renderer
Objective:
Produce markdown summaries from graph results.

Files:
- Create: agent/architecture_intel/report.py

Steps:
1. Port markdown report rendering.
2. Keep output concise and terminal-friendly.
3. Ensure no secrets/raw env dumps are included.

Verification:
- report.md writes successfully
- content includes summary, hotspots, bridges, sample paths

### Task 5: Add CLI surface in NOVA
Objective:
Expose build/report/hotspots/bridges/path commands from the real CLI.

Files:
- Modify: actual CLI main/commands registration file(s)

Steps:
1. Register `inspect` command group.
2. Add `build`, `report`, `hotspots`, `bridges`, `path` subcommands.
3. Wire analyzer execution to outputs.
4. Keep behavior aligned with nova-oss where possible.

Verification:
- CLI help shows inspect commands
- each command runs locally

### Task 6: Add unit and integration tests
Objective:
Lock in the port with repo-native tests.

Files:
- Create: tests/unit/test_architecture_intel.py
- Create/Modify: tests/integration/test_architecture_cli.py

Steps:
1. Add small temp-repo unit test for save/load/report.
2. Add live-repo graph build assertions for known key nodes.
3. Add CLI integration smoke tests.

Verification:
- targeted tests pass
- full relevant test subset passes

### Task 7: Real-repo validation on NOVA
Objective:
Prove usefulness on the actual runtime.

Steps:
1. compile changed modules
2. run targeted tests
3. run inspect build/report/hotspots/bridges/path on real repo
4. validate at least 3 meaningful architecture paths

Suggested path checks:
- main -> AIAgent
- AIAgent -> model_tools
- AIAgent -> tools.terminal_tool
- gateway.run -> gateway.platforms.telegram

Verification:
- graph emitted
- report emitted
- path queries produce useful outputs

### Task 8: Document results in KB and repo docs
Objective:
Capture what changed and what was learned.

Files:
- docs/reports/YYYY-MM-DD-nova-port-validation.md
- relevant KB project/fix pages

Verification:
- port status documented
- next limitations clearly listed

## Acceptance criteria

The port is complete when:
- NOVA has a native inspect command surface
- commands run on the real runtime repo
- tests pass
- report + graph artifacts are generated
- at least 3 real architectural paths are recovered
- no dependency on nova-oss package internals exists

## Expected follow-up after port

Once ported, next likely steps are:
1. inspect-assisted debugging/report workflows inside Hermes
2. richer bridge/hotspot scoring
3. optional incremental indexing
4. agent-facing architecture query helper built on top of this graph
