"""M5 tests for continuous metrics and experiment timeline."""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.mysql_metrics import MySQLMetricsProvider
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.slow_log import SlowLogTracker
from db_repro_agent.metrics.system_metrics import SystemMetricsProvider
from db_repro_agent.metrics.timeline import ExperimentPhase, ExperimentTimeline
from db_repro_agent.models.evidence import SnapshotBoundary
from db_repro_agent.tools.database.base import ToolResult, utc_now
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig


class CountingProvider:
    def __init__(self) -> None:
        self.count = 0

    def collect(self) -> dict[str, float]:
        self.count += 1
        return {"value": float(self.count)}

    def snapshot(self) -> dict[str, float]:
        return {"value": 100.0 + self.count}


class SlowCounterAdapter:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)

    def execute_readonly(self, sql, params=None):
        return ToolResult.ok(
            [{"Variable_name": "Slow_queries", "Value": str(next(self.values))}], utc_now(), utc_now()
        )


def test_timeline_records_warmup_baseline_injection_recovery_and_stop(tmp_path: Path) -> None:
    timeline = ExperimentTimeline()
    timeline.mark(ExperimentPhase.BASELINE, name="baseline_start")
    timeline.add_sample("qps", 100)
    timeline.mark(ExperimentPhase.INJECTION, name="injection_start", metadata={"action_id": "a1"})
    timeline.add_sample("qps", 50)
    timeline.mark(ExperimentPhase.RECOVERY, name="recovery_start")
    timeline.add_sample("qps", 90)
    timeline.mark(ExperimentPhase.STOPPED, name="workload_stop")

    output = timeline.write_jsonl(tmp_path / "metrics.jsonl")
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    phases = [record["phase"] for record in records]

    assert {"warmup", "baseline", "injection", "recovery", "stopped"}.issubset(set(phases))
    assert any(record.get("name") == "injection_start" for record in records)
    assert all("timestamp" in record for record in records)
    assert all("metric_name" in record and "value" in record for record in records if record["record_type"] == "sample")


def test_sampler_continues_collecting_while_main_thread_changes_phase() -> None:
    provider = CountingProvider()
    timeline = ExperimentTimeline()
    sampler = MetricsSampler(MetricsCollector({"test": provider}), interval_seconds=0.01)

    sampler.start(timeline, phase=ExperimentPhase.BASELINE)
    time.sleep(0.06)
    sampler.set_phase(ExperimentPhase.INJECTION, name="manual_injection_start")
    time.sleep(0.06)
    sampler.set_phase(ExperimentPhase.RECOVERY)
    time.sleep(0.04)
    sampler.stop()

    assert provider.count >= 8
    assert len(timeline.samples) >= 8
    assert any(sample.phase == ExperimentPhase.INJECTION for sample in timeline.samples)
    assert timeline.current_phase == ExperimentPhase.STOPPED


def test_boundary_snapshot_is_separate_from_periodic_timeline_sampling() -> None:
    provider = CountingProvider()
    timeline = ExperimentTimeline()
    collector = MetricsCollector({"test": provider})

    collector.collect_once(timeline, phase=ExperimentPhase.BASELINE)
    snapshot = collector.snapshot(SnapshotBoundary.PRE_INJECTION)

    assert snapshot.success is True
    assert snapshot.boundary == SnapshotBoundary.PRE_INJECTION
    assert snapshot.metrics["test.value"] == 101.0
    assert len(timeline.samples) == 1


def test_slow_log_uses_marker_delta_not_historical_total() -> None:
    tracker = SlowLogTracker(SlowCounterAdapter([100, 105]))

    marker = tracker.mark()
    values = tracker.collect(marker)

    assert marker.total_count == 100
    assert values["slow_queries_total"] == 105
    assert values["slow_queries_delta"] == 5


def test_system_metrics_provider_supports_deterministic_reader() -> None:
    provider = SystemMetricsProvider(
        reader=lambda: {
            "cpu_percent": 20,
            "memory_percent": 45,
            "io_read_bytes_per_sec": 100,
            "io_write_bytes_per_sec": 50,
        }
    )

    values = provider.collect()

    assert values == {
        "cpu_percent": 20.0,
        "memory_percent": 45.0,
        "io_read_bytes_per_sec": 100.0,
        "io_write_bytes_per_sec": 50.0,
    }


def test_mysql_provider_converts_counters_to_rates() -> None:
    class FakeAdapter:
        calls = 0

        def execute_readonly(self, sql, params=None):
            self.calls += 1
            base = 100 if self.calls == 1 else 130
            rows = [
                {"Variable_name": "Threads_connected", "Value": "3"},
                {"Variable_name": "Threads_running", "Value": "1"},
                {"Variable_name": "Questions", "Value": str(base)},
                {"Variable_name": "Com_commit", "Value": str(base)},
                {"Variable_name": "Com_rollback", "Value": "0"},
                {"Variable_name": "Innodb_row_lock_current_waits", "Value": "2"},
                {"Variable_name": "Innodb_row_lock_waits", "Value": "12"},
                {"Variable_name": "Innodb_row_lock_time", "Value": "3500"},
                {"Variable_name": "Innodb_buffer_pool_reads", "Value": "7"},
                {"Variable_name": "Innodb_buffer_pool_read_requests", "Value": "1000"},
                {"Variable_name": "Slow_queries", "Value": "7"},
                {"Variable_name": "Innodb_log_waits", "Value": "4"},
                {"Variable_name": "Innodb_os_log_pending_fsyncs", "Value": "0"},
                {"Variable_name": "Innodb_os_log_pending_writes", "Value": "0"},
                {"Variable_name": "Max_used_connections", "Value": "3"},
                {"Variable_name": "Aborted_connects", "Value": "0"},
            ]
            return ToolResult.ok(rows, utc_now(), utc_now())

    provider = MySQLMetricsProvider(FakeAdapter())
    first = provider.collect()
    second = provider.collect()

    assert first["threads_connected"] == 3
    assert first["qps"] == 0
    assert second["qps"] > 0
    assert second["tps"] > 0
    assert second["lock_waits"] == 12
    assert second["lock_waits_current"] == 2
    assert second["lock_wait_time_ms"] == 3500
    assert second["innodb_buffer_pool_reads"] == 7
    assert second["innodb_buffer_pool_read_requests"] == 1000


def _live_adapter_or_skip() -> MySQLAdapter:
    config = MySQLConfig.from_env()
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"local MySQL is not reachable: {exc}")
    return MySQLAdapter(config)


@pytest.mark.integration
def test_live_mysql_metrics_can_be_sampled_into_timeline() -> None:
    adapter = _live_adapter_or_skip()
    timeline = ExperimentTimeline()
    collector = MetricsCollector({"mysql": MySQLMetricsProvider(adapter)})

    collector.collect_once(timeline, phase=ExperimentPhase.BASELINE)
    time.sleep(0.05)
    timeline.mark(ExperimentPhase.INJECTION, name="injection_start")
    collector.collect_once(timeline)

    names = {sample.metric_name for sample in timeline.samples}
    assert "mysql.threads_connected" in names
    assert "mysql.threads_running" in names
    assert "mysql.qps" in names
