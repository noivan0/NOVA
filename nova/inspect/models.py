from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NodeRecord:
    id: str
    kind: str
    name: str
    qualname: str
    file_path: str
    lineno: int
    end_lineno: int
    module: str
    layer: str
    degree_in: int = 0
    degree_out: int = 0
    degree_total: int = 0
    bridge_score: int = 0


@dataclass
class EdgeRecord:
    source: str
    target: str
    kind: str


@dataclass
class PathQueryResult:
    source: str
    target: str
    path: List[str] = field(default_factory=list)
    found: bool = False


@dataclass
class AnalysisGraph:
    repo_root: str
    generated_at: str
    nodes: List[NodeRecord]
    edges: List[EdgeRecord]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "generated_at": self.generated_at,
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AnalysisGraph":
        return cls(
            repo_root=payload["repo_root"],
            generated_at=payload["generated_at"],
            nodes=[NodeRecord(**n) for n in payload.get("nodes", [])],
            edges=[EdgeRecord(**e) for e in payload.get("edges", [])],
            summary=payload.get("summary", {}),
        )

    def node_by_id(self, node_id: str) -> Optional[NodeRecord]:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None
