# NOVA Full Autonomy Guide

NOVA는 타이머 없이 **DB 변화 → 즉시 반응** 방식으로 자율 운영된다.

## 핵심 아키텍처

```
외부 입력 (LLM, 블로그, 크론 트리거 등)
        ↓
  hermes_events 테이블 (nova_brain.db)
        ↓
  nova_brain_watcher.py  ← inotify/watchdog 감시
        ↓
    이벤트 분류 → 액션 디스패치
        ↓
  ┌──────────────────────────────────────┐
  │  nova_dream.py        (판단 생성)    │
  │  nova_learn_engine.py (학습 수확)    │
  │  nova_chain_engine.py (에이전트 연결)│
  │  nova_wiki_synthesize.py (KB 합성)   │
  │  nova_autonomous_engine.py (종합)    │
  └──────────────────────────────────────┘
        ↓
  nova_brain.db pages / takes  ← 결과 저장
        ↓
  다음 이벤트 발생 → 사이클 반복
```

## 1. 두뇌-기억-수족 사이클

| 계층 | 구성 요소 | 역할 |
|------|----------|------|
| 두뇌 | Hermes Agent (LLM) | 판단·결정·오케스트레이션 |
| 기억 | nova_brain.db | pages, takes, hermes_events, nova_learn |
| 수족 | nova/agents/ 에이전트들 | 두뇌가 위임한 작업 실행 |
| 신경 | nova_chain_engine.py | 에이전트 간 신호 전달 |
| 감시 | nova_brain_watcher.py | DB 변화 감지 → 즉시 반응 |

## 2. 전체 에이전트 목록

### Core Brain Agents (nova/agents/bin/)

| 에이전트 | 역할 |
|---------|------|
| nova_brain.py | DB CRUD, 임베딩 검색, index-all |
| nova_brain_cli.py | CLI 인터페이스 (takes add, pages search 등) |
| nova_brain_embed.py | 벡터 임베딩 기반 유사도 검색 |
| nova_brain_hook.py | DB 변화 훅 트리거 |
| nova_brain_schema.py | DB 스키마 초기화 / 마이그레이션 |
| nova_brain_synthesize.py | takes → 종합 판단 생성 |
| nova_calibration.py | 자율화 캘리브레이션 (weight 조정) |
| nova_codex_gate.py | Codex/GPT 게이트 (코드 실행 위임) |
| nova_doctor.py | 시스템 상태 진단 |
| nova_dream.py | 판단 → dream takes 생성 (고수준 인사이트) |
| nova_emotional.py | 톤/감정 조정 레이어 |
| nova_kb_claim_extract.py | KB에서 클레임/사실 추출 |
| nova_kb_sync.py | KB ↔ nova_brain.db 동기화 |
| nova_learn_harvester.py | nova_learn → KB 변환 |
| nova_llm.py | LLM 추상화 레이어 |
| nova_migrate_ct.py | DB 마이그레이션 유틸 |
| nova_on_done_takes.py | takes 완료 시 훅 실행 |
| nova_search.py | pages/takes 통합 검색 |
| nova_takes_agent.py | takes 자율 에이전트 (HIGH 우선 처리) |
| nova_wiki_synthesize.py | wiki 자동 합성 |

### Autonomous Engine Agents (nova/agents/scripts/)

| 에이전트 | 역할 |
|---------|------|
| nova_autonomous_engine.py | 종합 자율화 엔진 (5단계 파이프라인) |
| nova_autonomous_engine_daemon.py | 데몬 래퍼 (비활성 권장 — watcher 우선) |
| nova_autonomous_loop.py | 단순 루프 (개발/테스트용) |
| nova_brain_watchdog.py | DB 헬스 체크 |
| nova_brain_watcher.py | **핵심** — inotify 기반 이벤트 감시 |
| nova_chain_engine.py | 에이전트 체인 실행 엔진 |
| nova_codex_gate.py | scripts용 Codex 게이트 |
| nova_db_status.py | DB 상태 빠른 확인 |
| nova_growth_tracker.py | 성장 지표 추적 |
| nova_hermes_briefing.py | 세션 시작 시 상태 브리핑 |
| nova_kanban_hook.py | Kanban 태스크 완료 훅 |
| nova_kb_sync.py | scripts용 KB 동기화 |
| nova_learn_engine.py | 학습 파이프라인 엔진 |
| nova_phase0.py | Phase0 초기화 (최초 실행) |
| nova_resource_collector.py | 외부 리소스 수집기 |
| nova_resource_updater.py | 리소스 업데이트 |

## 3. 이벤트 흐름 상세

### nova_brain_watcher.py 이벤트 타입

```python
# DB 감시 대상
nova_brain.db  → inotify CLOSE_WRITE → 이벤트 분류

이벤트 타입:
  "kb-published"    → wiki synthesize 트리거
  "takes-new"       → dream engine 트리거
  "resource-update" → resource updater 트리거
  "learn-done"      → learn harvester 트리거
  "chain-done"      → 다음 체인 단계 트리거
```

### hermes_events 테이블 구조

```sql
CREATE TABLE hermes_events (
  id         INTEGER PRIMARY KEY,
  event_type TEXT NOT NULL,     -- 이벤트 종류
  payload    TEXT,              -- JSON 페이로드
  source     TEXT,              -- 발생 에이전트
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  processed  INTEGER DEFAULT 0  -- 0=미처리, 1=처리완료
);
```

## 4. nova_brain.db KB 계층

```
L1 dream        최고수준 판단/인사이트
L2 synthesize   복수 takes의 종합 분석
L3 takes        개별 판단 기록
L4 learn        학습 수확 데이터
L5 kb           마크다운 KB 페이지
L6 wiki         상호연결 개념 위키
L7 resources    외부 수집 리소스
L8 chain        에이전트 체인 실행 로그
```

## 5. 자율화 시작 순서

```bash
# 1. 환경 설정
export HERMES_HOME=$HOME/.hermes
source $HERMES_HOME/.env

# 2. 브레인 와처 시작 (백그라운드 — 이벤트 엔진 핵심)
python3 $HERMES_HOME/scripts/nova_brain_watcher.py &

# 3. 상태 브리핑 확인
python3 $HERMES_HOME/scripts/nova_hermes_briefing.py

# 4. 자율화 엔진 시작
python3 $HERMES_HOME/scripts/nova_autonomous_engine.py

# 5. (선택) 크론으로 24/7 운영
# crontab -e:
#   */5 * * * * HERMES_HOME=$HOME/.hermes python3 $HOME/.hermes/scripts/nova_autonomous_engine.py
```

## 6. KB 연계 (llm-wiki 패턴)

NOVA는 마크다운 KB를 자동으로 nova_brain.db와 동기화한다.

```bash
# KB 파일 → DB 동기화
python3 $HERMES_HOME/bin/nova_kb_sync.py

# DB → KB 파일 역방향 동기화
python3 $HERMES_HOME/bin/nova_brain.py index-all

# KB 검색
python3 $HERMES_HOME/bin/nova_search.py "검색어"
```

### KB 디렉토리 구조

```
$HERMES_HOME/kb/
  config/          운영 정책, 채널 설정
  agents/          에이전트 프로파일
  projects/        프로젝트별 KB
  nova/            NOVA 자율화 관련
    learnings/     학습 수확 결과
  audit_loop/      감사 루프 기록
$HERMES_HOME/wiki/ 상호연결 개념 위키
```

## 7. 에이전트 프로파일 (SOUL.md)

각 에이전트는 `$HERMES_HOME/profiles/{name}/` 디렉토리를 가진다:

```
profiles/
  nova-research/
    SOUL.md        에이전트 정체성/역할 정의
    harness.md     작업 하네스
    evolution.md   학습/성장 기록
    config.yaml    LLM 설정
  nova-dev/
    SOUL.md
    ...
```

SOUL.md 예시:
```markdown
# Nova Research Agent

role: researcher
specialty: web research, arxiv, KB building
tools: [web_search, web_extract, read_file, write_file]

## Mission
KB에 지식을 축적하고 nova_brain.db에 학습 데이터를 공급한다.
```

## 8. 트러블슈팅

### watcher가 반응하지 않을 때
```bash
# inotify 한도 확인
cat /proc/sys/fs/inotify/max_user_watches

# 한도 늘리기
echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### DB 잠금 오류
```bash
python3 $HERMES_HOME/bin/nova_doctor.py
```

### 로그 확인
```bash
tail -f $HERMES_HOME/logs/nova_brain_watcher.log
tail -f $HERMES_HOME/logs/nova_autonomous_engine.log
```
