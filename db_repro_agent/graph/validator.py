"""Structural validation for an AnomalyGraph."""

from __future__ import annotations

from dataclasses import dataclass

from ..models.anomaly_graph import AnomalyGraph


@dataclass(frozen=True)
class GraphValidationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class GraphValidationResult:
    valid: bool
    issues: tuple[GraphValidationIssue, ...] = ()


class AnomalyGraphValidator:
    """Validate references and acyclicity without changing the graph."""

    def validate(self, graph: AnomalyGraph) -> GraphValidationResult:
        issues: list[GraphValidationIssue] = []
        node_ids = [node.node_id for node in graph.nodes]
        node_set = set(node_ids)
        if len(node_ids) != len(node_set):
            issues.append(GraphValidationIssue("DUPLICATE_NODE", "node_id values must be unique"))
        if graph.root_nodes is not None:
            for root in graph.root_nodes:
                if root not in node_set:
                    issues.append(GraphValidationIssue("UNKNOWN_ROOT", f"unknown root node: {root}"))

        edge_pairs: set[tuple[str, str]] = set()
        adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_set}
        indegree = {node_id: 0 for node_id in node_set}
        for edge in graph.edges:
            if edge.source not in node_set:
                issues.append(GraphValidationIssue("UNKNOWN_SOURCE", f"unknown source node: {edge.source}"))
            if edge.target not in node_set:
                issues.append(GraphValidationIssue("UNKNOWN_TARGET", f"unknown target node: {edge.target}"))
            pair = (edge.source, edge.target)
            if pair in edge_pairs:
                issues.append(GraphValidationIssue("DUPLICATE_EDGE", f"duplicate edge: {edge.source}->{edge.target}"))
            edge_pairs.add(pair)
            if edge.source in node_set and edge.target in node_set and edge.target not in adjacency[edge.source]:
                adjacency[edge.source].add(edge.target)
                indegree[edge.target] += 1

        queue = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for target in sorted(adjacency[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
                    queue.sort()
        if visited != len(node_set):
            issues.append(GraphValidationIssue("CYCLE", "anomaly graph must be acyclic"))
        return GraphValidationResult(valid=not issues, issues=tuple(issues))

    def assert_valid(self, graph: AnomalyGraph) -> None:
        result = self.validate(graph)
        if not result.valid:
            details = "; ".join(f"{issue.code}: {issue.message}" for issue in result.issues)
            raise ValueError(details)


def validate_graph(graph: AnomalyGraph) -> GraphValidationResult:
    return AnomalyGraphValidator().validate(graph)
