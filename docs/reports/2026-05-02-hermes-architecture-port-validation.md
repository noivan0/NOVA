# Hermes Architecture Intelligence Port Validation

Date: 2026-05-02
Repo: $NOVA_HOME

## Objective
Validate that the architecture intelligence capability proven in NOVA-oss is now present and runnable in the Hermes runtime codebase.

## Discovery result
The target repo already contains a native port:
- agent/architecture_intel/__init__.py
- agent/architecture_intel/analyzer.py
- agent/architecture_intel/models.py
- agent/architecture_intel/report.py
- hermes_cli/architecture.py
- CLI registration in hermes_cli/main.py under `architecture`

So this phase became validation/integration confirmation rather than first implementation.

## What was verified
1. CLI command registration exists in `hermes_cli/main.py`
   - architecture build
   - architecture summary
   - architecture hotspots
   - architecture bridges
   - architecture path
   - architecture report

2. Analyzer runs successfully on the live Hermes repo
   - output dir: `.hermes-arch/`
   - graph.json generated
   - summary.json generated

3. CLI smoke test succeeded
   - `python -m hermes_cli.main architecture build --repo-root .`

4. Path query validation
   - success: `run_agent -> model_tools`
   - success: `run_agent -> tools.terminal_tool`
   - partial success via adapter creation path:
     `gateway.run -> gateway.platforms`
     resolved to
     `module:gateway.run -> class:gateway.run.GatewayRunner -> function:gateway.run.GatewayRunner._create_adapter -> class:gateway.platforms.api_server.APIServerAdapter`

5. Summary and hotspot queries succeeded
   - Nodes: 26269
   - Edges: 71917

## Important finding
The Hermes runtime already had the architecture_intel port present before this phase. The immediate need was not initial porting but validating whether the port was wired and operational.

## Constraint encountered
Targeted pytest selection still failed during global collection due to unrelated existing collection/import problems in the repo, including:
- missing `normalize_anthropic_response`
- missing `_MAX_MEDIA_DOWNLOAD_BYTES` in `gateway/platforms/weixin.py`
- missing `_chat_content_to_responses_parts`
- missing `_is_blocked_ip`

These errors are outside architecture_intel itself and block clean targeted pytest collection unless isolated further or fixed separately.

## Verdict
Architecture intelligence is already ported and operational in Hermes runtime.

Immediate next value is not re-porting, but either:
1. add dedicated architecture_intel tests that can run in isolation despite broader collection breakage, or
2. fix the unrelated test-collection breakages so validation can be captured in pytest.

## Recommended next action
Create isolated tests for the Hermes architecture command path and/or repair the unrelated collection blockers, then re-run focused validation and record passing automated coverage.
