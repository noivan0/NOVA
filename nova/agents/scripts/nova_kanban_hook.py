#!/usr/bin/env python3
"""nova_kanban_hook.py — Hermes 에이전트 tool 호출 후처리 훅
post_tool_call 이벤트에서 kanban 상태 자동 업데이트.
"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import sys, json, sqlite3, re, time
from pathlib import Path

BOARDS_JSON = Path.home() / ".hermes" / "kanban" / "nova_boards.json"


def get_active_boards():
    try:
        data = json.loads(BOARDS_JSON.read_text())
        return data.get("boards", [])
    except Exception:
        return []


def post_tool_call(tool_name: str, output: str):
    """도구 호출 결과에서 kanban 이벤트 감지"""
    try:
        boards = get_active_boards()
        for board in boards:
            db_path = f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db"
            db = sqlite3.connect(db_path)
            try:
                c = db.cursor()

                # pytest 결과에서 passed/failed 감지
                if tool_name == "terminal":
                    m_pass = re.search(r"(\d+) passed", output)
                    m_fail = re.search(r"(\d+) failed", output)
                    if m_pass:
                        passed = int(m_pass.group(1))
                        failed = int(m_fail.group(1)) if m_fail else 0
                        # 현재 running 태스크에 결과 메모
                        now_ts = int(time.time())
                        if failed == 0:
                            c.execute("""
                                UPDATE tasks SET result=?, updated_at=?
                                WHERE status='running' AND (assignee='nova-qa' OR assignee='nova-dev')
                                LIMIT 1
                            """, (f"pytest: {passed} passed, {failed} failed", now_ts))
                        if failed == 0 and passed > 0:
                            # Auto-transition nova-qa running to done
                            c.execute("""
                                UPDATE tasks SET status='done', result=?, completed_at=?
                                WHERE status='running' AND assignee='nova-qa'
                            """, (f'{passed} tests passed', int(time.time())))
                            if c.rowcount > 0:
                                print(f"[hook] nova-qa {c.rowcount}개 done 자동 전환")
                        db.commit()
            finally:
                db.close()
    except Exception:
        pass  # 훅 실패는 조용히 무시


if __name__ == "__main__":
    # CLI 사용법: python3 nova_kanban_hook.py <tool_name> <output>
    # 예) python3 nova_kanban_hook.py terminal "5 passed, 0 failed"
    if len(sys.argv) >= 2:
        _tool_name = sys.argv[1]
        _output = sys.argv[2] if len(sys.argv) > 2 else ""
        post_tool_call(_tool_name, _output)
    else:
        print("usage: nova_kanban_hook.py <tool_name> [output]")
        sys.exit(1)
