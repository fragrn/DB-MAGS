"""Tests for sort/temp spill window-delta evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from anomalychains.cases import ChainCase
from anomalychains.run_cases import _observations
from db_repro_agent.metrics.timeline import ExperimentPhase, ExperimentTimeline


def test_sort_spill_observation_uses_window_deltas_not_cumulative_ratio() -> None:
    timeline = ExperimentTimeline(started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    start = timeline.started_at
    timeline.add_sample("mysql.created_tmp_disk_tables", 10, phase=ExperimentPhase.BASELINE, timestamp=start)
    timeline.add_sample("mysql.sort_merge_passes", 20, phase=ExperimentPhase.BASELINE, timestamp=start)
    timeline.add_sample("mysql.created_tmp_disk_tables", 12, phase=ExperimentPhase.BASELINE, timestamp=start + timedelta(seconds=1))
    timeline.add_sample("mysql.sort_merge_passes", 22, phase=ExperimentPhase.BASELINE, timestamp=start + timedelta(seconds=1))
    timeline.add_sample("mysql.created_tmp_disk_tables", 15, phase=ExperimentPhase.INJECTION, timestamp=start + timedelta(seconds=2))
    timeline.add_sample("mysql.sort_merge_passes", 25, phase=ExperimentPhase.INJECTION, timestamp=start + timedelta(seconds=2))
    timeline.add_sample("mysql.created_tmp_disk_tables", 17, phase=ExperimentPhase.OBSERVING, timestamp=start + timedelta(seconds=3))
    timeline.add_sample("mysql.sort_merge_passes", 27, phase=ExperimentPhase.OBSERVING, timestamp=start + timedelta(seconds=3))
    timeline.add_sample("mysql.created_tmp_disk_tables", 100, phase=ExperimentPhase.RECOVERY, timestamp=start + timedelta(seconds=4))
    timeline.add_sample("mysql.sort_merge_passes", 100, phase=ExperimentPhase.RECOVERY, timestamp=start + timedelta(seconds=4))

    case = ChainCase(
        "synthetic_sort_spill",
        ("improper_sql", "sort_hash_spill", "slow_query"),
        "runnable",
        "synthetic",
        "improper_sql",
    )
    observations = _observations(timeline, case)
    metrics = observations[0].metrics

    assert metrics["created_tmp_disk_tables_delta"] == 5
    assert metrics["sort_merge_passes_delta"] == 5
    assert metrics["temp_table_ratio"] == 2.5
    assert metrics["sort_merge_passes_ratio"] == 2.5
