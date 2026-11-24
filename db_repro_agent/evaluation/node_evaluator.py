"""Evaluate synthetic anomaly-node observations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models.anomaly_graph import AnomalyNode


class NodeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    hit: bool | None = None
    timestamp_sec: float | None = Field(default=None, ge=0.0)
    metrics: dict[str, float | str | bool] = Field(default_factory=dict)


class NodeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    hit: bool
    evaluated: bool = True
    detected_at_sec: float | None = None
    metrics: dict[str, float | str | bool] = Field(default_factory=dict)
    reason: str


class NodeEvaluator:
    def evaluate(self, node: AnomalyNode, observation: NodeObservation | None) -> NodeEvaluation:
        if observation is None:
            return NodeEvaluation(node_id=node.node_id, hit=False, reason="node observation is missing")
        hit = observation.hit if observation.hit is not None else infer_node_hit(node.node_id, observation.metrics)
        return NodeEvaluation(
            node_id=node.node_id,
            hit=hit,
            detected_at_sec=observation.timestamp_sec,
            metrics=dict(observation.metrics),
            reason="node evidence hit" if hit else "node evidence miss",
        )


def infer_node_hit(node_id: str, metrics: dict[str, float | str | bool]) -> bool:
    """Apply deterministic rules to downstream-node metrics."""
    values = {str(key): value for key, value in metrics.items()}
    name = node_id.lower()
    if name in {"connections_up", "threads_concurrency_up"}:
        return _number(values, "threads_connected_ratio") > 1.2 or _number(values, "threads_connected_delta") > 0
    if name == "row_lock":
        return (
            _number(values, "row_lock_waits_delta") > 0
            or _number(values, "row_lock_current_waits") > 0
            or _number(values, "lock_waits_delta") > 0
            or _number(values, "lock_waits_current") > 0
        )
    if name == "slow_query":
        return _number(values, "slow_queries_delta") > 0 or _number(values, "slow_queries_total_delta") > 0
    if name == "qps_drop":
        return _number(values, "qps_ratio") < 0.7
    if name == "query_duration":
        return _number(values, "query_duration_ms") >= 100
    if name in {"metadata_lock", "metadata_lock_wait"}:
        return _truthy(values.get("metadata_lock_evidence")) or _number(values, "metadata_lock_wait_count") > 0
    if name == "table_blocking":
        return _number(values, "table_blocking_wait_count") > 0 or _number(values, "table_blocking_current_count") > 0
    if name in {"missing_index", "poor_plan"}:
        return _number(values, "rows_examined_ratio") > 1 or _contains(values, "explain_access_type", "all") or _contains_any(values, "explain_extra", ("filesort", "temporary"))
    if name == "improper_sql":
        return _number(values, "rows_examined_ratio") > 3 or _number(values, "p95_latency_ratio") > 1 or _number(values, "slow_query_delta") > 0
    if name == "sort_hash_spill":
        return _number(values, "created_tmp_disk_tables_delta") > 0 or _number(values, "sort_merge_passes_delta") > 0
    if name in {"large_temp_table", "temp_table_spill"}:
        return _number(values, "created_tmp_disk_tables_delta") > 0 or _number(values, "sort_merge_passes_delta") > 0 or _number(values, "temp_table_ratio") > 1 or _number(values, "sort_merge_passes_ratio") > 1
    if name in {"resource_cpu", "resource_bottleneck_cpu"}:
        return _number(values, "cpu_usage_ratio") > 1 or _number(values, "load_average_1m") > 1 or _truthy(values.get("load_average_high"))
    if name in {"resource_io", "resource_bottleneck_io", "disk_saturation"}:
        return _number(values, "io_wait_ratio") > 1 or _number(values, "disk_util") >= 0.8
    if name == "resource_memory":
        return _number(values, "memory_usage_ratio") > 1 or _number(values, "buffer_pool_read_ratio") > 1 or _number(values, "innodb_buffer_pool_reads_delta") > 0
    if name == "buffer_pool_pressure":
        miss_ratio = _number(values, "buffer_pool_miss_ratio")
        baseline_miss_ratio = _number(values, "buffer_pool_baseline_miss_ratio")
        physical_reads = _number(values, "buffer_pool_physical_reads_per_sec")
        baseline_physical_reads = _number(values, "buffer_pool_baseline_physical_reads_per_sec")
        return (
            _truthy(values.get("buffer_pool_miss_ratio_valid"))
            and _truthy(values.get("buffer_pool_baseline_miss_ratio_valid"))
            and _truthy(values.get("buffer_pool_physical_reads_valid"))
            and _truthy(values.get("buffer_pool_baseline_physical_reads_valid"))
            and miss_ratio >= 0.05
            and miss_ratio >= baseline_miss_ratio * 2
            and physical_reads >= 1
            and physical_reads >= baseline_physical_reads * 2
        )
    if name in {"redo_log_pressure", "redo_log_flush_stall", "commit_latency_up"}:
        return _number(values, "innodb_log_waits_delta") > 0 or _number(values, "write_latency_ratio") > 1 or _number(values, "tps_ratio") < 1
    if name == "write_throughput_drop":
        return _number(values, "tps_ratio") < 0.7
    if name in {"connection_pressure", "connection_storm"}:
        return _number(values, "threads_connected_ratio") > 1 or _number(values, "max_used_connections_ratio") > 1 or _number(values, "connection_error_delta") > 0 or _number(values, "threads_connected_delta") > 0
    if name == "timeout":
        return _number(values, "timeout_error_count") > 0 or _number(values, "connection_error_delta") > 0 or _number(values, "aborted_connects_delta") > 0
    if name in {"deadlock_detected", "deadlock_storm"}:
        return _number(values, "deadlock_delta") > 0 or _number(values, "innodb_deadlocks") > 0
    if name in {"network_latency", "network_stall"}:
        return _number(values, "network_error_delta") > 0 or _number(values, "connection_error_delta") > 0 or _number(values, "qps_ratio") < 0.7
    return False


def _number(values: dict[str, float | str | bool], key: str) -> float:
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes", "on", "hit"}


def _contains(values: dict[str, float | str | bool], key: str, expected: str) -> bool:
    return expected in str(values.get(key, "")).lower()


def _contains_any(values: dict[str, float | str | bool], key: str, expected: tuple[str, ...]) -> bool:
    actual = str(values.get(key, "")).lower()
    return any(item in actual for item in expected)
