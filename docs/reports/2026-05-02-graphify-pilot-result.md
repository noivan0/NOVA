# NOVA-oss Graphify Pilot Result

## Pilot status
- Status: completed
- Date: 2026-05-02
- Target repo: `$NOVA_HOME`
- Graphify version: `graphifyy 0.6.4`
- Run command that worked: `graphify update .`

## Operational notes
- `graphify .` did not work because this CLI build does not expose the full-build command directly in terminal mode.
- `graphify update .` successfully performed AST/code extraction and produced the core outputs.
- Output footprint: `956K`
- Produced files:
  - `graphify-out/graph.json`
  - `graphify-out/GRAPH_REPORT.md`
  - `graphify-out/graph.html`
  - `graphify-out/manifest.json`
  - `graphify-out/.graphify_root`

## Build result
- Rebuilt successfully
- 270 nodes
- 577 edges
- 14 communities reported by runtime rebuild log
- `GRAPH_REPORT.md` summary reports 11 communities (tool/report inconsistency worth noting)

## Evidence gathered
### 1. Central abstractions surfaced clearly
Graph report god nodes:
1. `KB` - 31 edges
2. `LLMConfig` - 30 edges
3. `Orchestrator` - 30 edges
4. `PublisherConfig` - 26 edges
5. `Checkpoint` - 25 edges
6. `NotifierConfig` - 23 edges
7. `EvolutionLog` - 23 edges
8. `NOVAConfig` - 21 edges
9. `HarnessLoader` - 19 edges
10. `HarnessDefinition` - 17 edges

This aligns well with NOVA's real architecture: config/provider abstractions, orchestrator core, checkpoint/evolution, and KB are indeed central.

### 2. CLI-to-core flow was recoverable with direct path evidence
Shortest-path validation from graph:
- `main()` -> `HarnessLoader`
- `main()` -> `Orchestrator`
- `main()` -> `KB`
- `main()` -> `load_config()`
- `Orchestrator` -> `Checkpoint`
- `Orchestrator` -> `EvolutionLog`
- `Orchestrator` -> `KB`

This is directly useful for onboarding and architecture review.

### 3. Bridge nodes were identified well
Graph report suggested high-betweenness bridge questions around:
- `Orchestrator`
- `KB`
- `LLMConfig`

That is directionally useful because those really are cross-cutting nodes in NOVA.

## Weaknesses found
### 1. Query/explain quality is noisy for ambiguous labels
Examples:
- `graphify explain "Orchestrator"` matched `tests/integration/test_orchestrator_echo.py` instead of the `Orchestrator` class.
- `graphify explain "KB"` matched `tests/unit/test_kb.py` instead of the `KB` class.

This means the built-in fuzzy match can easily land on the wrong node unless exact IDs or post-filtering are used.

### 2. Some paths are semantically distorted by inferred test nodes
Example:
- `Orchestrator -> LLMConfig` shortest path went through `_make_orch()` in tests instead of the primary production path.

So graph queries are useful, but answers must be sanity-checked against source code.

### 3. Report consistency issue
- Rebuild log said `14 communities`
- `GRAPH_REPORT.md` said `11 communities`

This does not block usage, but reduces confidence in polished reporting.

### 4. Current run is code-centric only
This pilot used `graphify update .`, which gave good code-structure results but did not demonstrate strong doc-to-code semantic linking. So the pilot validates structure mapping more than mixed knowledge mapping.

## Evaluation against pilot criteria
### Criterion 1: usable outputs without harming repo state
- Pass
- Repo remained clean except expected new output directories/files.

### Criterion 2: at least 2 structurally useful insights beyond README/docs alone
- Pass
Additive insights:
1. Ranked god-node view immediately highlighted KB, LLMConfig, Orchestrator, Checkpoint, EvolutionLog as structural core.
2. Path queries quickly confirmed CLI -> loader/config/orchestrator/KB wiring without manually traversing multiple files.
3. Bridge-node framing identified which abstractions are cross-community join points.

### Criterion 3: support at least 3 architecture questions with graph evidence
- Pass
Answered with graph evidence:
1. Most central abstractions in NOVA
2. CLI flow into load_config/HarnessLoader/Orchestrator/KB
3. Orchestrator relationships to Checkpoint/EvolutionLog/KB

### Criterion 4: operational overhead low enough for selective use
- Pass with caveat
Pros:
- install and rerun were straightforward after finding working command
- output size under 1 MB on this repo
Cons:
- command UX is confusing
- ambiguous matching reduces trust in direct explain/query use

## Verdict for NOVA
- Recommendation: selective adoption recommended
- Best role: architecture exploration and repo onboarding aid
- Not recommended as canonical documentation source

## Adoption implication for Hermes / NOVA broader use
### Recommended
- Use Graphify optionally on medium/large repos when you need:
  - centrality/god-node overview
  - cross-module path tracing
  - initial architecture mapping before writing KB notes

### Not recommended
- Do not replace KB, memory, or llm-wiki with it.
- Do not trust raw query/explain output without code verification when labels are ambiguous.
- Do not run always-on for every small project.

## Concrete proposed operating mode
1. Run Graphify only on selected repos (`nova-oss`, Hermes codebase, other large frameworks).
2. Keep outputs out of git by default unless explicitly needed for review artifacts.
3. Use it before architecture review / merge-risk analysis / onboarding docs.
4. Convert validated findings into KB pages or docs; do not treat graph output as final truth.
5. For Hermes later, start with repo-level pilot only after confirming ignore rules and query hygiene.

## Bottom line
Graphify proved useful on NOVA-oss as a structure-discovery layer.
It is worth keeping as an optional analysis tool for NOVA/Hermes, but not as a replacement for KB, memory, or llm-wiki.