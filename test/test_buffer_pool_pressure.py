"""Unit tests for Buffer Pool counter deltas and pressure evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_repro_agent.evaluation.node_evaluator import NodeEvaluator, NodeObservation
from db_repro_agent.metrics.buffer_pool import (
    derive_buffer_pool_delta,
    summarize_buffer_pool_window,
)
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.timeline import ExperimentPhase, ExperimentTimeline, MetricSample
from db_repro_agent.models.anomaly_graph import AnomalyNode


def _sample(name: str, value: float, timestamp: datetime, phase: ExperimentPhase) -> MetricSample:
    return MetricSample(metric_name=name, value=value, timestamp=timestamp, phase=phase)


def _hit_metrics() -> dict[str, float | bool]:
    return {
        "buffer_pool_miss_ratio": 0.10,
        "buffer_pool_miss_ratio_valid": True,
        "buffer_pool_baseline_miss_ratio": 0.05,
        "buffer_pool_baseline_miss_ratio_valid": True,
        "buffer_pool_physical_reads_per_sec": 4.0,
        "buffer_pool_physical_reads_valid": True,
        "buffer_pool_baseline_physical_reads_per_sec": 2.0,
        "buffer_pool_baseline_physical_reads_valid": True,
    }


def test_adjacent_buffer_pool_counter_delta() -> None:
    result = derive_buffer_pool_delta(100, 110, 1000, 1200, 2.0)

    assert result.reads_delta == 10
    assert result.requests_delta == 200
    assert result.miss_ratio == 0.05
    assert result.physical_reads_per_sec == 5.0
    assert result.valid is True


def test_zero_request_delta_is_invalid() -> None:
    result = derive_buffer_pool_delta(100, 110, 1000, 1000, 1.0)

    assert result.miss_ratio is None
    assert result.physical_reads_per_sec is None
    assert result.valid is False


def test_counter_reset_does_not_create_negative_evidence() -> None:
    result = derive_buffer_pool_delta(100, 90, 1000, 900, 1.0)

    assert result.reads_delta == 0
    assert result.requests_delta == 0
    assert result.valid is False


def test_sampler_records_per_interval_buffer_pool_metrics() -> None:
    timeline = ExperimentTimeline()
    sampler = MetricsSampler(MetricsCollector({}))
    sampler.timeline = timeline
    sampler._record_buffer_pool_delta(
        {
            "mysql.innodb_buffer_pool_reads": 100,
            "mysql.innodb_buffer_pool_read_requests": 1000,
        }
    )
    sampler._record_buffer_pool_delta(
        {
            "mysql.innodb_buffer_pool_reads": 110,
            "mysql.innodb_buffer_pool_read_requests": 1200,
        }
    )

    names = {sample.metric_name for sample in timeline.samples}
    assert "mysql.buffer_pool_miss_ratio" in names
    assert "mysql.buffer_pool_miss_ratio_valid" in names
    assert "mysql.physical_reads_per_sec" in names


def test_window_summary_uses_counter_totals_and_excludes_recovery() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples = [
        _sample("mysql.innodb_buffer_pool_reads", 100, start, ExperimentPhase.BASELINE),
        _sample("mysql.innodb_buffer_pool_read_requests", 1000, start, ExperimentPhase.BASELINE),
        _sample("mysql.innodb_buffer_pool_reads", 110, start + timedelta(seconds=1), ExperimentPhase.BASELINE),
        _sample("mysql.innodb_buffer_pool_read_requests", 1200, start + timedelta(seconds=1), ExperimentPhase.BASELINE),
        _sample("mysql.innodb_buffer_pool_reads", 130, start + timedelta(seconds=2), ExperimentPhase.INJECTION),
        _sample("mysql.innodb_buffer_pool_read_requests", 1400, start + timedelta(seconds=2), ExperimentPhase.INJECTION),
        _sample("mysql.innodb_buffer_pool_reads", 150, start + timedelta(seconds=3), ExperimentPhase.OBSERVING),
        _sample("mysql.innodb_buffer_pool_read_requests", 1600, start + timedelta(seconds=3), ExperimentPhase.OBSERVING),
        _sample("mysql.innodb_buffer_pool_reads", 1000, start + timedelta(seconds=4), ExperimentPhase.RECOVERY),
        _sample("mysql.innodb_buffer_pool_read_requests", 2000, start + timedelta(seconds=4), ExperimentPhase.RECOVERY),
    ]

    baseline = summarize_buffer_pool_window(samples, {ExperimentPhase.BASELINE})
    active = summarize_buffer_pool_window(samples, {ExperimentPhase.INJECTION, ExperimentPhase.OBSERVING})

    assert baseline["miss_ratio"] == 0.05
    assert baseline["miss_ratio_valid"] is True
    assert active["miss_ratio"] == 0.1
    assert active["miss_ratio_valid"] is True
    assert active["physical_reads_per_sec"] == 20.0


def test_buffer_pool_pressure_requires_all_thresholds() -> None:
    evaluator = NodeEvaluator()
    node = AnomalyNode(node_id="buffer_pool_pressure")

    assert evaluator.evaluate(node, NodeObservation(node_id=node.node_id, metrics=_hit_metrics())).hit is True

    for key, value in (
        ("buffer_pool_miss_ratio", 0.049),
        ("buffer_pool_miss_ratio", 0.09),
        ("buffer_pool_physical_reads_per_sec", 0.9),
        ("buffer_pool_physical_reads_per_sec", 3.9),
    ):
        metrics = _hit_metrics()
        metrics[key] = value
        assert evaluator.evaluate(node, NodeObservation(node_id=node.node_id, metrics=metrics)).hit is False


def test_buffer_pool_pressure_rejects_invalid_or_memory_only_evidence() -> None:
    evaluator = NodeEvaluator()
    node = AnomalyNode(node_id="buffer_pool_pressure")

    invalid = _hit_metrics()
    invalid["buffer_pool_baseline_miss_ratio_valid"] = False
    assert evaluator.evaluate(node, NodeObservation(node_id=node.node_id, metrics=invalid)).hit is False

    memory_only = {"memory_usage_ratio": 2.0}
    assert evaluator.evaluate(node, NodeObservation(node_id=node.node_id, metrics=memory_only)).hit is False
