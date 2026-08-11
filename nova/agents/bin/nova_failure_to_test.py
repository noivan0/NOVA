#!/usr/bin/env python3
"""
nova_failure_to_test.py — CHAIN_FAIL 로그 → 영구 테스트 자동 변환
원칙: "Any failure you do not turn into a permanent test, you will meet again." — @hanakoxbt
실행: python3 nova_failure_to_test.py [--since 2026-08-01]
"""
import sqlite3, json, os, sys
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path(os.environ.get('NOVA_HOME',   str(Path.home()/'.nova')))
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes')))
TEST_DIR    = HERMES_HOME / 'tests' / 'failures'
TEST_DIR.mkdir(parents=True, exist_ok=True)

def extract_failures(since: str = None) -> list:
    """실패 이벤트 추출: brain.db events + agent_activity 테이블"""
    db_path = str(NOVA_HOME/'brain.db')
    if not Path(db_path).exists():
        print(f'[failure_to_test] brain.db 없음: {db_path}')
        return []

    conn = sqlite3.connect(db_path, timeout=5)
    failures = []

    # hermes_events에서 FAIL 이벤트
    try:
        query = "SELECT event_type, title, detail, created_at FROM hermes_events WHERE event_type LIKE '%FAIL%'"
        params = []
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT 50"
        rows = conn.execute(query, params).fetchall()
        for r in rows:
            failures.append({'source':'hermes_events','type':r[0],'title':r[1],'detail':r[2],'at':r[3]})
    except Exception as e:
        print(f'[failure_to_test] hermes_events 조회 스킵: {e}')

    # agent_activity에서 FAIL/ERROR result
    try:
        query2 = "SELECT agent, action, result, target_path, created_at FROM agent_activity WHERE result LIKE '%FAIL%' OR result LIKE '%ERROR%'"
        params2 = []
        if since:
            query2 += " AND created_at >= ?"
            params2.append(since)
        query2 += " ORDER BY created_at DESC LIMIT 30"
        rows2 = conn.execute(query2, params2).fetchall()
        for r in rows2:
            failures.append({'source':'agent_activity','agent':r[0],'action':r[1],'result':r[2],'path':r[3],'at':r[4]})
    except Exception as e:
        print(f'[failure_to_test] agent_activity 조회 스킵: {e}')

    conn.close()
    return failures

def failure_to_test(failure: dict) -> dict:
    """실패 이벤트 → 4줄 테스트 레코드 (Eval Engineering Step4 형식)"""
    return {
        'what_agent_did':   failure.get('action', failure.get('type', 'unknown')),
        'worked_or_failed': 'FAILED',
        'agent_or_external': 'agent' if failure.get('agent') else 'system',
        'capability_to_protect': failure.get('title', failure.get('result', ''))[:120],
        'source': failure.get('source'),
        'occurred_at': failure.get('at'),
        'detail': json.dumps(failure, ensure_ascii=False)[:500],
    }

def main():
    since = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg.startswith('--since'):
            since = arg.split('=', 1)[-1] if '=' in arg else (args[i+1] if i+1 < len(args) else None)

    failures = extract_failures(since)
    if not failures:
        print('[failure_to_test] 실패 이벤트 없음')
        return

    saved = 0
    for f in failures:
        test = failure_to_test(f)
        ts = (f.get('at','') or datetime.now(timezone.utc).isoformat())[:19].replace(':','-')
        agent = f.get('agent', f.get('source', 'unknown'))
        # 파일명 안전 처리 (슬래시 등 제거)
        agent_safe = str(agent).replace('/', '_').replace('\\', '_')
        fname = f'{agent_safe}_{ts}.json'
        fpath = TEST_DIR / fname
        if not fpath.exists():
            fpath.write_text(json.dumps(test, ensure_ascii=False, indent=2))
            saved += 1

    print(f'[failure_to_test] 완료: {len(failures)}개 이벤트 → {saved}개 신규 테스트 저장')
    print(f'  저장 위치: {TEST_DIR}')

if __name__ == '__main__':
    main()
