import tempfile
from pathlib import Path

from nova.inspect import ArchitectureAnalyzer, render_markdown_report


def test_architecture_build_detects_core_nodes():
    analyzer = ArchitectureAnalyzer(Path(__file__).resolve().parents[2])
    graph = analyzer.build()
    ids = {node.id for node in graph.nodes}

    assert "class:nova.core.orchestrator.Orchestrator" in ids
    assert "class:nova.core.harness.HarnessLoader" in ids
    assert "class:nova.core.checkpoint.Checkpoint" in ids
    assert "class:nova.core.evolution.EvolutionLog" in ids
    assert "class:nova.core.kb.KB" in ids
    assert "class:nova.core.config.LLMConfig" in ids


def test_architecture_paths_recover_expected_flow():
    analyzer = ArchitectureAnalyzer(Path(__file__).resolve().parents[2])
    graph = analyzer.build()

    path_main_to_orch = analyzer.find_path(graph, "main", "Orchestrator")
    assert path_main_to_orch.found is True

    path_orch_to_ckpt = analyzer.find_path(graph, "Orchestrator", "Checkpoint")
    assert path_orch_to_ckpt.found is True


def test_architecture_save_and_report():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "app.py").write_text(
            "from pkg.core import Engine\n\n"
            "def main():\n    engine = Engine()\n    return engine.run()\n"
        )
        (root / "pkg" / "core.py").write_text(
            "class Engine:\n    def run(self):\n        return 'ok'\n"
        )

        analyzer = ArchitectureAnalyzer(root)
        graph = analyzer.build()
        graph_path = analyzer.save(graph)
        loaded = analyzer.load()
        report = render_markdown_report(
            loaded,
            analyzer.top_hotspots(loaded),
            analyzer.top_bridges(loaded),
            [analyzer.find_path(loaded, "main", "Engine")],
        )

        assert graph_path.exists()
        assert (analyzer.output_dir / "summary.json").exists()
        assert loaded.summary["node_count"] == graph.summary["node_count"]
        assert "Top Hotspots" in report
