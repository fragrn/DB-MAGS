"""Deterministic root-node selection."""

from __future__ import annotations

from ..models.anomaly_graph import AnomalyGraph


def select_roots(graph: AnomalyGraph) -> list[str]:
    if graph.root_nodes is not None:
        return sorted(graph.root_nodes)
    targets = {edge.target for edge in graph.edges}
    return sorted(node.node_id for node in graph.nodes if node.node_id not in targets)


class RootSelector:
    def select(self, graph: AnomalyGraph) -> list[str]:
        return select_roots(graph)
