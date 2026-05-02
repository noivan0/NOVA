# NOVA-oss Graphify Pilot Plan

> Goal: test whether Graphify adds practical structure-discovery value on NOVA-oss beyond existing README/docs/KB-style materials, and use the evidence to decide whether Hermes/NOVA should adopt it later.

## Scope
- Target repo: $NOVA_HOME
- Compare Graphify outputs against existing human-authored docs:
  - README.md
  - docs/architecture.md
  - code layout under nova/
  - tests/
- No production Hermes integration yet

## Success criteria
1. Graphify must produce usable outputs on NOVA-oss without breaking repo state.
2. It must surface at least 2 structurally useful insights faster or more clearly than README/docs alone.
3. It must support at least 3 concrete architecture questions with graph evidence.
4. Operational overhead must stay low enough for selective use on medium/large repos.

## Evaluation rubric
### A. Output quality
- Does GRAPH_REPORT identify real central modules/god nodes?
- Does wiki/index provide useful navigation?
- Do graph queries return source-grounded paths instead of vague summaries?

### B. Incremental value vs existing docs
- Does it reveal relationships not obvious from README/docs?
- Does it reduce raw-file hunting for cross-module questions?
- Does it help map code-to-doc alignment?

### C. Operational cost
- Install friction
- Build time / rerun friction
- Extra output footprint and git noise
- Need for ignore patterns

### D. Adoption implication
- Best fit as: replacement, supplement, or unnecessary
- Candidate usage mode: always-on, project-level optional, or ad hoc analysis only

## Test questions
1. What are the most central abstractions in NOVA?
2. How does CLI flow into harness loading, orchestration, checkpointing, evolution logging, and KB writes?
3. How are provider abstractions wired from config to execution?
4. Does the graph show meaningful linkage between docs and code, or mostly code-only structure?
5. Is the output more useful for architecture review than existing docs alone?

## Procedure
1. Install Graphify locally.
2. Run Graphify on NOVA-oss with minimal invasive setup.
3. Capture produced outputs:
   - graphify-out/GRAPH_REPORT.md
   - graphify-out/wiki/index.md
   - graphify-out/graph.json presence
4. Run targeted graph queries for the test questions.
5. Compare findings with README/docs and direct code inspection.
6. Judge recommendation for NOVA and future Hermes adoption.

## Decision thresholds
- Recommend selective adoption if criteria 1-4 pass and at least 2 practical insights are clearly additive.
- Recommend against adoption if outputs are mostly redundant with README/docs or maintenance overhead outweighs gains.
