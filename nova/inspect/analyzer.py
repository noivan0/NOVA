from __future__ import annotations

import ast
import json
from collections import Counter, defaultdict, deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import AnalysisGraph, EdgeRecord, NodeRecord, PathQueryResult


class ArchitectureAnalyzer:
    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).resolve()
        self.output_dir = self.repo_root / ".nova-arch"

    def build(self) -> AnalysisGraph:
        py_files = self._python_files()
        nodes: List[NodeRecord] = []
        edges: List[EdgeRecord] = []
        symbol_index: Dict[str, str] = {}
        module_nodes: Dict[str, str] = {}
        pending_call_edges: List[Tuple[str, str]] = []
        pending_inherit_edges: List[Tuple[str, str]] = []
        pending_import_edges: List[Tuple[str, str]] = []

        for path in py_files:
            module_name = self._module_name(path)
            layer = self._guess_layer(path)
            module_id = f"module:{module_name}"
            module_node = NodeRecord(
                id=module_id,
                kind="module",
                name=Path(module_name).name,
                qualname=module_name,
                file_path=self._rel(path),
                lineno=1,
                end_lineno=self._file_end_lineno(path),
                module=module_name,
                layer=layer,
            )
            nodes.append(module_node)
            module_nodes[module_name] = module_id
            symbol_index[module_name] = module_id

            tree = ast.parse(path.read_text(), filename=str(path))
            module_visitor = _ModuleVisitor(module_name)
            module_visitor.visit(tree)

            for cls in module_visitor.classes:
                class_id = f"class:{cls['qualname']}"
                nodes.append(NodeRecord(
                    id=class_id,
                    kind="class",
                    name=cls["name"],
                    qualname=cls["qualname"],
                    file_path=self._rel(path),
                    lineno=cls["lineno"],
                    end_lineno=cls["end_lineno"],
                    module=module_name,
                    layer=layer,
                ))
                symbol_index[cls["qualname"]] = class_id
                symbol_index[cls["name"]] = class_id
                edges.append(EdgeRecord(source=module_id, target=class_id, kind="contains"))
                for base in cls["bases"]:
                    pending_inherit_edges.append((class_id, base))

            for fn in module_visitor.functions:
                fn_id = f"function:{fn['qualname']}"
                nodes.append(NodeRecord(
                    id=fn_id,
                    kind="function",
                    name=fn["name"],
                    qualname=fn["qualname"],
                    file_path=self._rel(path),
                    lineno=fn["lineno"],
                    end_lineno=fn["end_lineno"],
                    module=module_name,
                    layer=layer,
                ))
                symbol_index[fn["qualname"]] = fn_id
                symbol_index[fn["name"]] = fn_id
                parent_id = module_id
                if fn.get("parent_class"):
                    parent_id = symbol_index.get(f"{module_name}.{fn['parent_class']}", module_id)
                edges.append(EdgeRecord(source=parent_id, target=fn_id, kind="contains"))
                for called in fn["calls"]:
                    pending_call_edges.append((fn_id, called))

            for imported in module_visitor.imports:
                pending_import_edges.append((module_id, imported))

        for source_id, raw_target in pending_call_edges:
            target_id = self._resolve_symbol(raw_target, symbol_index)
            if target_id:
                edges.append(EdgeRecord(source=source_id, target=target_id, kind="calls"))

        for source_id, raw_target in pending_inherit_edges:
            target_id = self._resolve_symbol(raw_target, symbol_index)
            if target_id:
                edges.append(EdgeRecord(source=source_id, target=target_id, kind="inherits"))

        for source_id, raw_target in pending_import_edges:
            target_id = self._resolve_import(raw_target, module_nodes)
            if target_id:
                edges.append(EdgeRecord(source=source_id, target=target_id, kind="imports"))

        graph = self._enrich(nodes, edges)
        return graph

    def save(self, graph: AnalysisGraph) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        graph_path = self.output_dir / "graph.json"
        summary_path = self.output_dir / "summary.json"
        graph_path.write_text(json.dumps(graph.to_dict(), indent=2))
        summary_path.write_text(json.dumps(graph.summary, indent=2))
        return graph_path

    def load(self) -> AnalysisGraph:
        graph_path = self.output_dir / "graph.json"
        payload = json.loads(graph_path.read_text())
        return AnalysisGraph.from_dict(payload)

    def find_path(self, graph: AnalysisGraph, source_query: str, target_query: str) -> PathQueryResult:
        source = self._find_node_id(graph, source_query)
        target = self._find_node_id(graph, target_query)
        if not source or not target:
            return PathQueryResult(source=source_query, target=target_query, path=[], found=False)

        adjacency: Dict[str, List[str]] = defaultdict(list)
        for edge in graph.edges:
            adjacency[edge.source].append(edge.target)

        queue = deque([[source]])
        seen = {source}
        while queue:
            path = queue.popleft()
            node = path[-1]
            if node == target:
                return PathQueryResult(source=source, target=target, path=path, found=True)
            for nxt in adjacency.get(node, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(path + [nxt])
        return PathQueryResult(source=source, target=target, path=[], found=False)

    def top_hotspots(self, graph: AnalysisGraph, limit: int = 10) -> List[NodeRecord]:
        return sorted(graph.nodes, key=lambda n: (-n.degree_total, n.id))[:limit]

    def top_bridges(self, graph: AnalysisGraph, limit: int = 10) -> List[NodeRecord]:
        return sorted(graph.nodes, key=lambda n: (-n.bridge_score, -n.degree_total, n.id))[:limit]

    def _python_files(self) -> List[Path]:
        skip_dirs = {
            ".git",
            ".nova-arch",
            "__pycache__",
            ".venv",
            "venv",
            "node_modules",
            ".mypy_cache",
            ".pytest_cache",
            "build",
            "dist",
            "site-packages",
        }
        return [
            p for p in sorted(self.repo_root.rglob("*.py"))
            if not any(part in skip_dirs for part in p.parts)
        ]

    def _module_name(self, path: Path) -> str:
        rel = path.relative_to(self.repo_root)
        return ".".join(rel.with_suffix("").parts)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.repo_root))

    def _guess_layer(self, path: Path) -> str:
        rel = self._rel(path)
        if rel.startswith("nova/cli/"):
            return "cli"
        if rel.startswith("nova/core/"):
            return "core"
        if rel.startswith("nova/providers/"):
            return "provider"
        if rel.startswith("tests/"):
            return "test"
        return "other"

    def _file_end_lineno(self, path: Path) -> int:
        return len(path.read_text().splitlines())

    def _resolve_symbol(self, raw_target: str, symbol_index: Dict[str, str]) -> Optional[str]:
        if raw_target in symbol_index:
            return symbol_index[raw_target]
        short = raw_target.split(".")[-1]
        return symbol_index.get(short)

    def _resolve_import(self, raw_target: str, module_nodes: Dict[str, str]) -> Optional[str]:
        if raw_target in module_nodes:
            return module_nodes[raw_target]
        parts = raw_target.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in module_nodes:
                return module_nodes[candidate]
            parts.pop()
        return None

    def _enrich(self, nodes: List[NodeRecord], edges: List[EdgeRecord]) -> AnalysisGraph:
        indeg = Counter()
        outdeg = Counter()
        neighbors: Dict[str, Set[str]] = defaultdict(set)
        cross_module_neighbors: Dict[str, Set[str]] = defaultdict(set)
        node_map = {n.id: n for n in nodes}

        dedup = []
        seen_edges = set()
        for edge in edges:
            key = (edge.source, edge.target, edge.kind)
            if edge.source == edge.target or key in seen_edges:
                continue
            seen_edges.add(key)
            dedup.append(edge)
            indeg[edge.target] += 1
            outdeg[edge.source] += 1
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
            src_mod = node_map.get(edge.source).module if node_map.get(edge.source) else ""
            tgt_mod = node_map.get(edge.target).module if node_map.get(edge.target) else ""
            if src_mod and tgt_mod and src_mod != tgt_mod:
                cross_module_neighbors[edge.source].add(tgt_mod)
                cross_module_neighbors[edge.target].add(src_mod)

        enriched = []
        for node in nodes:
            new_node = replace(
                node,
                degree_in=indeg[node.id],
                degree_out=outdeg[node.id],
                degree_total=indeg[node.id] + outdeg[node.id],
                bridge_score=len(cross_module_neighbors[node.id]),
            )
            enriched.append(new_node)

        summary = {
            "node_count": len(enriched),
            "edge_count": len(dedup),
            "kinds": Counter(n.kind for n in enriched),
            "layers": Counter(n.layer for n in enriched),
            "edge_kinds": Counter(e.kind for e in dedup),
            "top_hotspots": [n.id for n in sorted(enriched, key=lambda n: (-n.degree_total, n.id))[:10]],
            "top_bridges": [n.id for n in sorted(enriched, key=lambda n: (-n.bridge_score, -n.degree_total, n.id))[:10]],
        }
        return AnalysisGraph(
            repo_root=str(self.repo_root),
            generated_at=datetime.now(timezone.utc).isoformat(),
            nodes=enriched,
            edges=dedup,
            summary=summary,
        )

    def _find_node_id(self, graph: AnalysisGraph, query: str) -> Optional[str]:
        lowered = query.lower()
        exact = [n.id for n in graph.nodes if n.id.lower() == lowered or n.qualname.lower() == lowered or n.name.lower() == lowered]
        if exact:
            return exact[0]
        partial = [n.id for n in graph.nodes if lowered in n.id.lower() or lowered in n.qualname.lower()]
        if partial:
            return sorted(partial)[0]
        return None


class _ModuleVisitor(ast.NodeVisitor):
    def __init__(self, module_name: str):
        self.module_name = module_name
        self.classes: List[Dict[str, object]] = []
        self.functions: List[Dict[str, object]] = []
        self.imports: List[str] = []
        self._class_stack: List[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append(node.module)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = f"{self.module_name}.{node.name}"
        bases = [_expr_name(base) for base in node.bases if _expr_name(base)]
        self.classes.append({
            "name": node.name,
            "qualname": qualname,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "bases": bases,
        })
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(self, node: ast.AST) -> None:
        name = node.name
        parent_class = self._class_stack[-1] if self._class_stack else None
        qualname = f"{self.module_name}.{name}"
        if parent_class:
            qualname = f"{self.module_name}.{parent_class}.{name}"
        self.functions.append({
            "name": name,
            "qualname": qualname,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "calls": _collect_calls(node),
            "parent_class": parent_class,
        })
        self.generic_visit(node)


def _collect_calls(node: ast.AST) -> List[str]:
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            called = _expr_name(child.func)
            if called:
                calls.append(called)
    return calls


def _expr_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None
