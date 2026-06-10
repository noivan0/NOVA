import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "nova.cli.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_inspect_build_and_path_commands():
    build = run_cli("inspect", "build", ".")
    assert build.returncode == 0
    assert "Built architecture graph:" in build.stdout
    assert (REPO_ROOT / ".nova-arch" / "graph.json").exists()

    # [EP-NOVA-001 FIX 2026-06-04] "main" 단독 사용 시 examples.kb_quickstart.main 과
    # nova.cli.main.main 중 알파벳순 첫 번째가 선택되어 경로 탐색 실패 (ambiguity)
    # → qualname 명시로 수정 (옵션 A: 안전, 구조 변경 없음)
    path = run_cli("inspect", "path", ".", "--from", "nova.cli.main.main", "--to", "Orchestrator")
    assert path.returncode == 0
    assert "Orchestrator" in path.stdout


def test_inspect_report_command():
    report = run_cli("inspect", "report", ".")
    assert report.returncode == 0
    assert (REPO_ROOT / ".nova-arch" / "report.md").exists()
