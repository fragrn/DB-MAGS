from __future__ import annotations

from types import SimpleNamespace

from db_repro_agent.evaluation.node_evaluator import NodeEvaluator, NodeObservation
from db_repro_agent.metrics.mysql_metrics import MySQLMetricsProvider
from db_repro_agent.tools.database.base import ToolResult
from db_repro_agent.tools.database.mysql import MySQLConfig
from anomalychains.cases import CASE_BY_ID


def test_lock_node_evidence_is_split() -> None:
    evaluator = NodeEvaluator()

    assert evaluator.evaluate(
        _node("row_lock"),
        NodeObservation(node_id="row_lock", metrics={"row_lock_waits_delta": 1}),
    ).hit
    assert evaluator.evaluate(
        _node("row_lock"),
        NodeObservation(node_id="row_lock", metrics={"row_lock_current_waits": 1}),
    ).hit
    assert not evaluator.evaluate(
        _node("row_lock"),
        NodeObservation(node_id="row_lock", metrics={"row_lock_waits_delta": 0}),
    ).hit

    assert evaluator.evaluate(
        _node("metadata_lock_wait"),
        NodeObservation(node_id="metadata_lock_wait", metrics={"metadata_lock_wait_count": 1}),
    ).hit
    assert not evaluator.evaluate(
        _node("metadata_lock_wait"),
        NodeObservation(node_id="metadata_lock_wait", metrics={"metadata_lock_wait_count": 0}),
    ).hit

    assert evaluator.evaluate(
        _node("table_blocking"),
        NodeObservation(node_id="table_blocking", metrics={"table_blocking_wait_count": 1}),
    ).hit
    assert evaluator.evaluate(
        _node("table_blocking"),
        NodeObservation(node_id="table_blocking", metrics={"table_blocking_current_count": 1}),
    ).hit
    assert not evaluator.evaluate(
        _node("table_blocking"),
        NodeObservation(node_id="table_blocking", metrics={"table_blocking_wait_count": 0}),
    ).hit


def test_propagation_cases_use_split_lock_nodes() -> None:
    assert CASE_BY_ID["06_hot_update_lock_slow_qps"].chain[1] == "row_lock"
    assert CASE_BY_ID["09_long_tx_lock_slow_qps"].chain[1] == "row_lock"
    assert CASE_BY_ID["11_metadata_lock_slow_qps"].chain[1] == "metadata_lock_wait"
    assert CASE_BY_ID["19_table_lock_wait_qps"].chain[1] == "table_blocking"
    assert CASE_BY_ID["20_traffic_threads_lock_slow_qps"].chain[2] == "row_lock"


def test_table_blocking_probe_degrades_without_stopping_sampling() -> None:
    provider = MySQLMetricsProvider(_FakeAdapter(table_blocking_available=False))
    metrics = provider.collect()
    assert metrics["table_blocking_wait_count"] == 0
    assert metrics["table_blocking_probe_degraded"] == 1


def test_table_blocking_probe_counts_pending_waiter_with_granted_blocker() -> None:
    provider = MySQLMetricsProvider(_FakeAdapter(table_blocking_available=True))
    metrics = provider.collect()
    assert metrics["metadata_lock_wait_count"] == 1
    assert metrics["table_blocking_wait_count"] == 1
    assert metrics["table_blocking_current_count"] == 1
    assert metrics["table_blocking_probe_degraded"] == 0


def _node(node_id: str):
    from db_repro_agent.models.anomaly_graph import AnomalyNode

    return AnomalyNode(node_id=node_id)


class _FakeAdapter:
    config = MySQLConfig()

    def __init__(self, *, table_blocking_available: bool) -> None:
        self.table_blocking_available = table_blocking_available

    def execute_readonly(self, sql, params=None):
        if "metadata_locks" in sql and not self.table_blocking_available:
            return SimpleNamespace(success=False, data=[], error_message="permission denied")
        if "metadata_locks" in sql and "EXISTS" in sql:
            data = [{"wait_count": 1}]
        elif "metadata_locks" in sql:
            data = [{"wait_count": 1}]
        else:
            data = [{"Variable_name": name, "Value": value} for name, value in {
                "Threads_connected": "1",
                "Threads_running": "1",
                "Questions": "1",
                "Com_commit": "1",
                "Com_rollback": "0",
                "Innodb_row_lock_waits": "0",
                "Innodb_row_lock_current_waits": "0",
                "Innodb_row_lock_time": "0",
                "Slow_queries": "0",
                "Innodb_log_waits": "0",
                "Innodb_os_log_pending_fsyncs": "0",
                "Innodb_os_log_pending_writes": "0",
                "Max_used_connections": "1",
                "Aborted_connects": "0",
            }.items()]
        return SimpleNamespace(success=True, data=data, error_message=None)
