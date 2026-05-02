# NOVA-oss Native Graphify Plan

> Goal: build a native, repo-local architecture intelligence capability inside NOVA-oss without depending on Graphify runtime outputs or code.

## Non-negotiable constraint
- NOVA and NOVA-oss must be worked on independently.
- No direct file copying between repos during initial implementation.
- NOVA-oss implementation becomes the reference experiment only.
- If adopted into NOVA later, implementation must be recreated/adapted independently, then only validated improvements may be backported intentionally.

## Product goal
Provide a lightweight native architecture-inspection layer that helps answer:
1. What are the most central abstractions?
2. What are the bridge nodes between major subsystems?
3. What are the shortest meaningful paths between entrypoints and core components?
4. What modules appear isolated or under-documented?

## Scope for Phase 1 in NOVA-oss
- Python-only static inspection
- No external LLM dependency
- No fuzzy free-text explain mode
- No multimodal ingestion
- No test-aware semantic inference beyond simple structural edges

## Deliverables
1. Native inspect package in NOVA-oss
2. CLI entrypoint under `nova inspect ...`
3. JSON graph artifact
4. Markdown report artifact
5. Tests proving graph build/report/path behavior
6. Evaluation report vs prior Graphify pilot

## Proposed commands
- `nova inspect build [path]`
- `nova inspect report [path]`
- `nova inspect path [path] --from X --to Y`
- `nova inspect hotspots [path]`
- `nova inspect bridges [path]`

## Core design
### Nodes
- file
- class
- function
- method

### Edges
- contains
- imports
- calls
- inherits

### Signals
- degree-based hotspots
- betweenness-based bridges
- shortest structural path
- isolated nodes/components

## Output location
Store artifacts under repo-local `.nova-inspect/` to avoid coupling with Graphify conventions.

## Validation criteria
- Build succeeds on NOVA-oss
- Report identifies real core abstractions comparable to prior pilot
- Path query recovers CLI -> config/orchestrator/KB relationships
- Test suite passes
- Outputs remain deterministic and low-noise

## If satisfactory
Phase 2: implement equivalent capability independently in NOVA, validate on NOVA-specific structure, then backport only proven improvements intentionally to NOVA-oss.
