"""
tests/unit/test_autonomous_loop_shell_safety.py — nova_autonomous_loop.py
셸 인용 안전성 검증 (2026-08-28, 확장 감사)

배경: v1.9.0 셸 인젝션 감사(Orchestrator._exec_shell)에서 발견한 패턴을
전체 코드베이스로 확장 조사한 결과, nova_autonomous_loop.py의
get_ready_tasks()/dispatch_task()도 f-string으로 board 값을 셸 명령에
직접 삽입하고 있었다. board는 nova_boards.json(로컬 설정파일)에서 오므로
LLM이 직접 통제하는 값은 아니지만, 동일한 방어적 원칙(값을 셸 명령에
삽입할 때는 반드시 이스케이프)을 적용해 강화했다.

이 파일도 shebang 앞에 `import os`가 잘못 위치하고 `from pathlib import
Path`가 누락되어 있어 import 시점에 NameError가 나는 버그를 발견해
함께 수정했다.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "nova" / "agents" / "scripts" / "nova_autonomous_loop.py"


def test_module_has_valid_syntax():
    ast.parse(MODULE_PATH.read_text())


def test_module_imports_without_nameerror(tmp_path, monkeypatch):
    """이전 버그: shebang 위치 오류로 `import os`가 파일 최상단에 있었고
    `from pathlib import Path`가 빠져 있어 Path.home() 호출 시점에
    NameError가 발생했다. 격리된 HERMES_HOME으로 import가 실제로
    성공하는지 확인 (module_from_spec으로 실제 로드)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("nova_autonomous_loop_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # NameError가 나면 여기서 실패
    assert hasattr(mod, "get_ready_tasks")
    assert hasattr(mod, "dispatch_task")


def test_get_ready_tasks_quotes_board_value(tmp_path, monkeypatch):
    """board 값에 셸 메타문자가 섞여도 shlex.quote()로 감싸져 명령
    구조를 깨지 못해야 한다."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("nova_autonomous_loop_test2", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return ""

    mod.run = fake_run
    malicious_board = 'board"; touch /tmp/pwned_board; echo "'
    mod.get_ready_tasks(malicious_board)

    assert len(calls) == 1
    # shlex.quote()가 적용되었으면 원본 위험 문자열이 그대로 노출되지 않고
    # 작은따옴표로 감싸진 단일 토큰이 되어야 한다.
    import shlex
    assert shlex.quote(malicious_board) in calls[0]


def test_dispatch_task_quotes_board_and_task_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_dir = tmp_path / "profiles" / "nova-dev"
    profile_dir.mkdir(parents=True)

    spec = importlib.util.spec_from_file_location("nova_autonomous_loop_test3", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return "dispatched"

    mod.run = fake_run
    mod.HERMES_HOME = str(tmp_path)

    malicious_board = 'x"; touch /tmp/pwned2; echo "'
    task = {"id": "t_abc123", "assignee": "nova-dev", "title": "test"}
    mod.dispatch_task(malicious_board, task)

    assert len(calls) == 1
    import shlex
    assert shlex.quote(malicious_board) in calls[0]
