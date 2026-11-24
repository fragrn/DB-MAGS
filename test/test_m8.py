"""M8 synthetic AnomalyGraph model and evaluator acceptance tests."""

from __future__ import annotations

import pytest

from db_repro_agent.evaluation.graph_evaluator import GraphEvaluator
from db_repro_agent.evaluation.node_evaluator import NodeObservation
from db_repro_agent.graph.root_selector import select_roots
from db_repro_agent.graph.validator import AnomalyGraphValidator
from db_repro_agent.models.anomaly_graph import AnomalyEdge, AnomalyGraph, AnomalyNode


def chain_graph(**edge_overrides) -> AnomalyGraph:
    return AnomalyGraph(
        graph_id="a-b-c",
        nodes=[AnomalyNode(node_id="A"), AnomalyNode(node_id="B"), AnomalyNode(node_id="C")],
        edges=[
            AnomalyEdge(source="A", target="B", mechanism="load", provenance="fixture", confidence=0.9, **edge_overrides),
            AnomalyEdge(source="B", target="C", mechanism="queue", provenance="fixture", confidence=0.9),
        ],
    )


def test_all_nodes_and_edges_hit_with_valid_lags() -> None:
    graph = chain_graph(min_lag_sec=1, max_lag_sec=5)
    result = GraphEvaluator().evaluate(
        graph,
        [
            NodeObservation(node_id="A", hit=True, timestamp_sec=10),
            NodeObservation(node_id="B", hit=True, timestamp_sec=13),
            NodeObservation(node_id="C", hit=True, timestamp_sec=18),
        ],
    )

    assert result.all_nodes_hit is True
    assert result.all_edges_satisfied is True
    assert [edge.lag_sec for edge in result.edge_results] == [3, 5]
    assert result.failed_node is None
    assert result.failed_edge is None
    assert result.longest_successful_prefix == ["A", "B", "C"]


def test_first_failed_edge_is_a_to_b_when_b_misses() -> None:
    result = GraphEvaluator().evaluate(
        chain_graph(),
        [
            NodeObservation(node_id="A", hit=True, timestamp_sec=10),
            NodeObservation(node_id="B", hit=False, timestamp_sec=None),
            NodeObservation(node_id="C", hit=False, timestamp_sec=None),
        ],
    )

    assert result.all_nodes_hit is False
    assert result.failed_node == "B"
    assert result.failed_edge == "A->B"
    assert result.longest_successful_prefix == ["A"]


def test_temporal_order_is_required() -> None:
    result = GraphEvaluator().evaluate(
        chain_graph(min_lag_sec=1),
        [
            NodeObservation(node_id="A", hit=True, timestamp_sec=10),
            NodeObservation(node_id="B", hit=True, timestamp_sec=5),
            NodeObservation(node_id="C", hit=True, timestamp_sec=18),
        ],
    )

    assert result.all_nodes_hit is True
    assert result.edge_results[0].temporal_valid is False
    assert result.edge_results[0].lag_sec == -5
    assert result.failed_edge == "A->B"
    assert result.longest_successful_prefix == ["A"]


def test_mechanism_metric_can_fail_an_edge() -> None:
    graph = chain_graph(mechanism_metrics={"lock_waits": 1})
    result = GraphEvaluator().evaluate(
        graph,
        [
            NodeObservation(node_id="A", hit=True, timestamp_sec=10),
            NodeObservation(node_id="B", hit=True, timestamp_sec=13, metrics={"lock_waits": 0}),
            NodeObservation(node_id="C", hit=True, timestamp_sec=18),
        ],
    )

    assert result.all_nodes_hit is True
    assert result.edge_results[0].mechanism_valid is False
    assert result.edge_results[0].satisfied is False
    assert result.failed_edge == "A->B"
    assert result.longest_successful_prefix == ["A"]


def test_graph_validator_rejects_bad_references_and_cycles() -> None:
    graph = AnomalyGraph(
        graph_id="invalid",
        nodes=[AnomalyNode(node_id="A"), AnomalyNode(node_id="B")],
        edges=[
            AnomalyEdge(source="A", target="B", mechanism="x", provenance="fixture", confidence=1),
            AnomalyEdge(source="B", target="A", mechanism="x", provenance="fixture", confidence=1),
        ],
    )
    validation = AnomalyGraphValidator().validate(graph)
    assert validation.valid is False
    assert any(issue.code == "CYCLE" for issue in validation.issues)


def test_roots_are_deterministic() -> None:
    assert select_roots(chain_graph()) == ["A"]


def test_missing_node_observation_is_a_node_miss() -> None:
    result = GraphEvaluator().evaluate(chain_graph(), [NodeObservation(node_id="A", hit=True, timestamp_sec=1)])
    assert result.node_results["B"].hit is False
    assert result.failed_node == "B"


def test_invalid_lag_window_is_rejected() -> None:
    with pytest.raises(ValueError):
        AnomalyEdge(
            source="A",
            target="B",
            mechanism="x",
            provenance="fixture",
            confidence=0.8,
            min_lag_sec=5,
            max_lag_sec=1,
        )

