"""Deterministic evaluator for a synthetic AnomalyGraph."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from ..graph.validator import AnomalyGraphValidator
from ..models.anomaly_graph import AnomalyGraph
from .edge_evaluator import EdgeEvaluation, EdgeEvaluator
from .node_evaluator import NodeEvaluation, NodeEvaluator, NodeObservation


class GraphEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: str
    node_results: dict[str, NodeEvaluation]
    edge_results: list[EdgeEvaluation]
    all_nodes_hit: bool
    all_edges_satisfied: bool
    failed_node: str | None = None
    failed_edge: str | None = None
    longest_successful_prefix: list[str] = Field(default_factory=list)


class GraphEvaluator:
    def __init__(self, node_evaluator: NodeEvaluator | None = None, edge_evaluator: EdgeEvaluator | None = None) -> None:
        self.node_evaluator = node_evaluator or NodeEvaluator()
        self.edge_evaluator = edge_evaluator or EdgeEvaluator()
        self.validator = AnomalyGraphValidator()

    def evaluate(
        self,
        graph: AnomalyGraph,
        observations: list[NodeObservation] | dict[str, NodeObservation],
        *,
        ignored_nodes: set[str] | None = None,
    ) -> GraphEvaluation:
        self.validator.assert_valid(graph)
        observation_map = observations if isinstance(observations, dict) else {item.node_id: item for item in observations}
        ignored = ignored_nodes or set()
        node_results = {
            node.node_id: self.node_evaluator.evaluate(node, observation_map.get(node.node_id))
            for node in graph.nodes
        }
        for node_id in ignored:
            if node_id in node_results:
                node_results[node_id] = node_results[node_id].model_copy(
                    update={
                        "hit": True,
                        "evaluated": False,
                        "detected_at_sec": node_results[node_id].detected_at_sec if node_results[node_id].detected_at_sec is not None else 0.0,
                        "reason": "root injection node evaluation skipped",
                    }
                )
        edge_results = []
        for edge in graph.edges:
            edge_results.append(self.edge_evaluator.evaluate(edge, node_results[edge.source], node_results[edge.target]))

        failed_node = next(
            (node.node_id for node in _topological_nodes(graph) if node.node_id not in ignored and not node_results[node.node_id].hit),
            None,
        )
        failed_edge = next((f"{edge.source}->{edge.target}" for edge in graph.edges if not edge_results[graph.edges.index(edge)].satisfied), None)
        prefix = _longest_prefix(graph, node_results, edge_results)
        return GraphEvaluation(
            graph_id=graph.graph_id,
            node_results=node_results,
            edge_results=edge_results,
            all_nodes_hit=all(result.hit for node_id, result in node_results.items() if node_id not in ignored),
            all_edges_satisfied=all(result.satisfied for result in edge_results),
            failed_node=failed_node,
            failed_edge=failed_edge,
            longest_successful_prefix=prefix,
        )


def _topological_nodes(graph: AnomalyGraph):
    incoming = {node.node_id: 0 for node in graph.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        incoming[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    queue = sorted(node_id for node_id, count in incoming.items() if count == 0)
    result = []
    by_id = {node.node_id: node for node in graph.nodes}
    while queue:
        current = queue.pop(0)
        result.append(by_id[current])
        for target in sorted(outgoing[current]):
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
                queue.sort()
    return result


def _longest_prefix(graph: AnomalyGraph, nodes: dict[str, NodeEvaluation], edges: list[EdgeEvaluation]) -> list[str]:
    if not graph.nodes:
        return []
    edge_by_pair = {(item.source, item.target): item for item in edges}
    prefix: list[str] = []
    for node in _topological_nodes(graph):
        if not nodes[node.node_id].hit:
            break
        incoming = [edge for edge in graph.edges if edge.target == node.node_id]
        if incoming and not all(edge_by_pair[(edge.source, edge.target)].satisfied for edge in incoming):
            break
        prefix.append(node.node_id)
    return prefix
