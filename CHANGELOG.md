# NOVA Changelog

---

## v3.0.0 — Agent OS 완전자율화 (2026-07-24)

### Major: NOVA Agent OS Phase 1~5 완성 + 마라톤 100회 완주 실증

**완전자율화 루프 구현 완료**
- brain_watcher 3레이어 구조 (@reboot 자동기동, inotify + kanban 이벤트 반응)
- 14단계 체인: autoplan → dev → review → cso → qa → ship → checkpoint → canary+health → evaluator → retro+learn → document → document-release → sysaudit → autoplan 재진입
- marathon 100회 자율 반복 실증 (takes 1,551 누적, health=100.0)

**버그 픽스 누적 (4차 감사까지)**
- BUG-CHAIN-FRESH: chain_engine ⑤번 블록 fresh_tasks 재조회 (파견 누락 방지)
- BUG-DOD-FAIL: code_implement harness dod_verify 키워드 앞부분 배치 필수
- BUG-HARNESS-SCRIPT: harness.py `script` 필드 폴백 (`ph.get("command", ph.get("script",""))`)
- BUG-ORCH-2ND-VAR: chain.py ORCH-2ND `ORCHESTRATOR_PY` 대문자 오타 → 소문자 수정
- BUG-KANBAN-PIPE: done 60개 초과 Broken pipe → archive_stale_tasks 자동 정리
- BUG-WAL-SPIN: brain_watcher CPU 100% → _DB_FILENAMES + TTL 3초 캐시 + 이벤트 레이트 리밋
- BUG-WORKSPACE-EXPAND: orchestrator.py workspace `expanduser()` 미적용 수정
- BUG-SYSAUDIT-REGISTER: nova_orchestrator.py HARNESS_AGENTS nova-sysaudit 등록
- STUCK-RECOVER: active=1 10분 고착 자동 복구 (`_recover_stuck_loop`)
- stalled(blocked) 태스크 역방향 점프 분리 (순환 폭발 방지)

**harnesses 21종 완성**
- canary, code_implement, code_review, document_gen, document_release
- go_nogo, health, investigate, kpi_evaluate, learn, mms_research, nuuseta_research
- qa, research(v2.0 실웹검색), retro, security_sign_off, ship
- summarizer, system_audit, verification_gate

**gstack + superpowers 내재화**
- nova-validator(verification_gate) 추가 — 완료 주장 전 신선 검증 강제 (Iron Law)
- nova-cso STRIDE/위협모델 통합 보안감사 트리거 확장
- nova-investigate 근본원인 5-whys 트리거 확장

**자가감사 (nova_self_audit.py)**
- 30개 CRITICAL/HIGH 점검 항목 자동화
- /roop 실행 전 선행 감사 필수 — FAIL 시 차단

---

## v2.0.0 — 완전자율화 첫 구현 (2026-07-10)

Claude Code + Codex 공동감사 + 마라톤 100회 완주

---

## v1.4.0 — Full Autonomous Release

## What's New

### Major: Full Agent Implementation Published

All 36 internal agents are now publicly available under `nova/agents/`:

**Core Brain Agents (nova/agents/bin/) — 20 agents**
- nova_brain.py — DB CRUD, embedding search, index-all
- nova_brain_cli.py — CLI interface
- nova_brain_embed.py — Vector embedding similarity search
- nova_brain_synthesize.py — takes → high-level synthesis
- nova_codex_gate.py — Code execution delegation
- nova_dream.py — Dream takes generation (top-level insight)
- nova_emotional.py — Tone/emotional layer
- nova_kb_claim_extract.py — KB claim extraction
- nova_kb_sync.py — KB ↔ DB sync
- nova_learn_harvester.py — Learn → KB conversion
- nova_takes_agent.py — Autonomous takes agent
- nova_wiki_synthesize.py — Wiki auto-synthesis
- nova_calibration.py, nova_doctor.py, nova_llm.py, nova_search.py, etc.

**Autonomous Engine Agents (nova/agents/scripts/) — 16 agents**
- nova_brain_watcher.py — **Core event engine** (inotify-based)
- nova_autonomous_engine.py — Full autonomy pipeline
- nova_chain_engine.py — Agent-to-agent relay
- nova_learn_engine.py — Learning pipeline
- nova_resource_collector.py — External resource collection
- nova_hermes_briefing.py — Session start briefing
- nova_growth_tracker.py, nova_kanban_hook.py, etc.

**Shell Scripts (nova/agents/shells/) — 9 scripts**
- nova_audit_loop.sh, nova_dream_runner.sh, nova_evaluator_daily.sh, etc.

### New: setup.sh
One-command installation for any Unix system:
```bash
git clone https://github.com/noivan0/NOVA && cd NOVA && bash setup.sh
```

### New: Full Autonomy Guide
`docs/guides/full-autonomy.md` — Complete architecture documentation including:
- Event flow diagrams
- DB schema reference
- KB hierarchy (L1~L8)
- Agent profile (SOUL.md) spec
- Systemd service setup
- Troubleshooting guide

### Changed: Path Portability
All agents now use `HERMES_HOME` environment variable instead of hardcoded `/root/.hermes`. Default: `~/.hermes`.

---

## v1.3.0 — OSS Framework Release

Initial public release with:
- nova Python package (engine, db, watcher, kb, inspect)
- 6 built-in engines (dream, learn, synthesize, chain, fix_orphan, memory_slim)
- nova setup / nova watcher CLI
- 82 tests passing
