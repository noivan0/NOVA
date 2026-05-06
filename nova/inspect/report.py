from __future__ import annotations

from typing import List

from .models import AnalysisGraph, NodeRecord, PathQueryResult


def render_markdown_report(
    graph: AnalysisGraph,
    hotspots: List[NodeRecord],
    bridges: List[NodeRecord],
    sample_paths: List[PathQueryResult],
) -> str:
    lines = [
        "# NOVA Architecture Intelligence Report",
        "",
        f"- Repo root: `{graph.repo_root}`",
        f"- Generated at: `{graph.generated_at}`",
        f"- Nodes: {graph.summary.get('node_count', 0)}",
        f"- Edges: {graph.summary.get('edge_count', 0)}",
        "",
        "## Summary",
        "",
        f"- Node kinds: {dict(graph.summary.get('kinds', {}))}",
        f"- Layers: {dict(graph.summary.get('layers', {}))}",
        f"- Edge kinds: {dict(graph.summary.get('edge_kinds', {}))}",
        "",
        "## Top Hotspots",
        "",
    ]
    for idx, node in enumerate(hotspots, start=1):
        lines.append(
            f"{idx}. `{node.id}` — degree={node.degree_total} "
            f"in={node.degree_in} out={node.degree_out} layer={node.layer}"
        )

    lines.extend(["", "## Top Bridges", ""])
    for idx, node in enumerate(bridges, start=1):
        lines.append(
            f"{idx}. `{node.id}` — bridge_score={node.bridge_score} "
            f"degree={node.degree_total} layer={node.layer}"
        )

    lines.extend(["", "## Sample Paths", ""])
    for result in sample_paths:
        if result.found:
            lines.append(f"- `{result.source}` -> `{result.target}`: {' -> '.join(result.path)}")
        else:
            lines.append(f"- `{result.source}` -> `{result.target}`: not found")

    return "\n".join(lines) + "\n"
