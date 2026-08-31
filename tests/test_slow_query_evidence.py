from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.evaluator import evaluate_node
from agent.executor import Executor
from agent.probes.mysql_probe import compare_statement_digest_latency, compare_statement_histograms
from agent.workload import _summarize_slow_sql_intervals, make_evaluation_pair, summarize_window


def _snapshot(counts, threshold_sec=1.0):
    bounds = [
        (0, 500_000_000_000),
        (500_000_000_000, 1_000_000_000_000),
        (1_100_000_000_000, 2_000_000_000_000),
    ]
    return {
        "available": True,
        "schema": "tpcc",
        "slow_threshold_sec": threshold_sec,
        "buckets": [
            {"timer_low_ps": low, "timer_high_ps": high, "count": count}
            for (low, high), count in zip(bounds, counts)
        ],
    }


def _digest_snapshot(rows, histogram_counts):
    bounds = [
        (0, 500_000_000_000),
        (500_000_000_000, 1_000_000_000_000),
        (1_000_000_000_000, 2_000_000_000_000),
    ]
    return {
        "available": True,
        "schema": "tpcc",
        "digests": [
            {
                "schema": "tpcc",
                "digest": digest,
                "digest_text": text,
                "count_star": count,
                "sum_timer_wait_ps": total,
                "max_timer_wait_ps": max_wait,
            }
            for digest, text, count, total, max_wait in rows
        ],
        "histograms": [
            {
                "digest": digest,
                "timer_low_ps": low,
                "timer_high_ps": high,
                "count": count,
            }
            for digest, counts in histogram_counts.items()
            for (low, high), count in zip(bounds, counts)
        ],
    }


class SlowQueryEvidenceTests(unittest.TestCase):
    def test_histogram_delta_uses_conservative_bucket_threshold(self):
        result = compare_statement_histograms(
            _snapshot([100, 20, 2]),
            _snapshot([180, 39, 7]),
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["total_statement_count"], 104)
        self.assertEqual(result["slow_statement_count"], 5)
        self.assertAlmostEqual(result["slow_ratio"], 5 / 104)
        self.assertEqual(result["effective_threshold_sec"], 1.1)

    def test_histogram_counter_reset_is_unavailable(self):
        result = compare_statement_histograms(
            _snapshot([100, 20, 2]),
            _snapshot([99, 21, 3]),
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["reset_detected"])

    def test_digest_latency_diff_outputs_top10_and_overall(self):
        before = _digest_snapshot(
            [
                ("d1", "SELECT * FROM order_line", 10, 1_000_000_000_000, 600_000_000_000),
                ("d2", "SELECT * FROM customer", 5, 500_000_000_000, 200_000_000_000),
            ],
            {"d1": [10, 0, 0], "d2": [5, 0, 0]},
        )
        after = _digest_snapshot(
            [
                ("d1", "SELECT * FROM order_line", 13, 7_000_000_000_000, 3_000_000_000_000),
                ("d2", "SELECT * FROM customer", 9, 1_300_000_000_000, 300_000_000_000),
            ],
            {"d1": [10, 2, 1], "d2": [7, 2, 0]},
        )

        result = compare_statement_digest_latency(before, after)

        self.assertTrue(result["available"])
        self.assertEqual(result["overall"]["count"], 7)
        self.assertEqual(result["top10"][0]["digest"], "d1")
        self.assertEqual(result["top10"][0]["execution_count"], 3)
        self.assertEqual(result["top10"][0]["avg_latency_ms"], 2000.0)
        self.assertEqual(result["overall"]["p95_latency_ms"], 2000.0)

    def test_summarize_window_includes_query_latency_fields(self):
        sample = {
            "db_metrics": {},
            "workload": {
                "qps": 10,
                "tps": 5,
                "performance_schema_slow_sql": _slow_metric(100, 1),
                "performance_schema_query_latency": {
                    "available": True,
                    "schema": "tpcc",
                    "top10": [{
                        "digest": "d1",
                        "digest_text": "SELECT * FROM order_line",
                        "execution_count": 2,
                        "avg_latency_ms": 1500.0,
                        "median_latency_ms": 1000.0,
                        "p95_latency_ms": 2000.0,
                        "max_latency_ms": 2200.0,
                        "total_latency_ms": 3000.0,
                    }],
                    "overall": {
                        "count": 2,
                        "avg_latency_ms": 1500.0,
                        "median_latency_ms": 1000.0,
                        "p95_latency_ms": 2000.0,
                        "max_latency_ms": 2200.0,
                        "total_latency_ms": 3000.0,
                    },
                },
            },
            "os_metrics": {},
        }

        summary = summarize_window([sample])

        self.assertEqual(summary["query_latency_top10"][0]["digest"], "d1")
        self.assertEqual(summary["query_latency_overall"]["avg_latency_ms"], 1500.0)

    def test_workload_pair_carries_phase_slow_sql_ratios(self):
        baseline_slow = _summarize_slow_sql_intervals([{
            "available": True,
            "schema": "tpcc",
            "slow_threshold_sec": 1.0,
            "effective_threshold_sec": 1.1,
            "total_statement_count": 100,
            "slow_statement_count": 1,
        }])
        injection_slow = _summarize_slow_sql_intervals([{
            "available": True,
            "schema": "tpcc",
            "slow_threshold_sec": 1.0,
            "effective_threshold_sec": 1.1,
            "total_statement_count": 100,
            "slow_statement_count": 3,
        }])
        baseline, injection = make_evaluation_pair(
            _window(baseline_slow),
            _window(injection_slow),
        )
        self.assertEqual(baseline["performance_schema_slow_sql"]["slow_ratio"], 0.01)
        self.assertEqual(injection["performance_schema_slow_sql"]["slow_ratio"], 0.03)

    def test_slow_query_hits_on_one_incremental_target_database_log_entry(self):
        evidence = _slow_log_evidence([{
            "database": "tpcc",
            "sql": "SELECT * FROM order_line",
            "query_time_sec": 12.0,
            "logging_reason": "query_time_gte_long_query_time",
        }])
        result = evaluate_node(
            "slow_query",
            {},
            {"slow_log_evidence": evidence},
            target_path=["traffic_surge", "slow_query"],
        )
        self.assertTrue(result.hit)
        self.assertEqual(result.evidence["evidence_mode"], "incremental_mysql_slow_log")
        self.assertEqual(result.evidence["long_query_time_at_injection_start"], "10.000000")
        self.assertEqual(result.evidence["log_queries_not_using_indexes_at_injection_start"], "OFF")
        self.assertEqual(result.evidence["logging_reason_counts"]["query_time_gte_long_query_time"], 1)

    def test_slow_query_does_not_use_other_metrics_without_log_entry(self):
        trace = _raw_trace(executions=8, p95_ms=12000.0, threshold_ms=10000.0)
        result = evaluate_node(
            "slow_query",
            {"slow_query_count_delta": 0, "performance_schema_slow_sql": _slow_metric(100, 0)},
            {
                "slow_query_count_delta": 99,
                "performance_schema_slow_sql": _slow_metric(100, 50),
                "slow_log_evidence": _slow_log_evidence([]),
            },
            execution_trace=trace,
            target_path=["traffic_surge", "slow_query"],
        )
        self.assertFalse(result.hit)
        self.assertIn("no new slow-log entry", result.details)

    def test_slow_query_is_not_hit_when_log_evidence_is_unavailable(self):
        result = evaluate_node(
            "slow_query",
            {},
            {"slow_log_evidence": {"available": False, "target_entry_count": 1}},
            target_path=["missing_index", "slow_query"],
        )
        self.assertFalse(result.hit)
        self.assertIn("unavailable", result.details)

    def test_executor_puts_action_summary_in_trace(self):
        executor = Executor(RuntimeConfig())
        action_result = {
            "kind": "raw_sql_workload",
            "executions": 8,
            "latency_ms": {"p95": 1200.0},
        }
        with patch.object(executor, "_run_action", return_value=action_result):
            trace = executor.execute({
                "tasks": {"raw": {"actions": [{"kind": "raw_sql_workload"}]}},
                "edges": [],
                "schedule": {},
            })
        self.assertEqual(trace.tasks["raw"].metrics["actions"][0]["result"], action_result)

    def test_raw_sql_writes_full_latency_artifact(self):
        class Cursor:
            def execute(self, sql):
                return None

            def fetchall(self):
                return []

            def close(self):
                return None

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = Executor(RuntimeConfig(), round_dir=tmpdir)
            with (
                patch("pymysql.connect", return_value=Connection()),
                patch("agent.executor.MySQLProbe.long_query_time_sec", return_value=0.1),
            ):
                result = executor._run_sql_workload(
                    {
                        "database": "tpcc",
                        "sql": "SELECT 1",
                        "concurrency": 1,
                        "duration_sec": 0.12,
                    },
                    task_id="raw task",
                    kind="raw_sql_workload",
                )
            artifact = Path(result["latency_artifact"])
            payload = json.loads(artifact.read_text())
        self.assertGreaterEqual(result["executions"], 5)
        self.assertEqual(len(payload["latencies_ms"]), result["executions"])
        self.assertEqual(result["slow_threshold_ms"], 100.0)
        self.assertIn("avg_ms", payload)
        self.assertIn("median_ms", result)
        self.assertEqual(payload["top10_slowest_ms"], sorted(payload["latencies_ms"], reverse=True)[:10])
        self.assertEqual(result["above_long_query_time_count"], payload["above_long_query_time_count"])

    def test_qps_drop_uses_only_average_qps_and_strict_seventy_percent_boundary(self):
        baseline, below = make_evaluation_pair(
            _window(_slow_metric(100, 0), qps=100.0, tps=100.0),
            _window(_slow_metric(100, 0), qps=69.0, tps=100.0),
        )
        hit = evaluate_node("qps_drop", baseline, below)
        self.assertTrue(hit.hit)

        baseline, boundary = make_evaluation_pair(
            _window(_slow_metric(100, 0), qps=100.0, tps=100.0),
            _window(_slow_metric(100, 0), qps=70.0, tps=0.0),
        )
        not_hit = evaluate_node("qps_drop", baseline, boundary)
        self.assertFalse(not_hit.hit)


def _slow_metric(total, slow):
    return {
        "available": True,
        "schema": "tpcc",
        "slow_threshold_sec": 1.0,
        "effective_threshold_sec": 1.1,
        "total_statement_count": total,
        "slow_statement_count": slow,
        "slow_ratio": slow / total,
    }


def _slow_log_evidence(entries):
    variables = {
        "slow_query_log": "ON",
        "log_output": "FILE",
        "slow_query_log_file": "/tmp/mysql-slow.log",
        "long_query_time": "10.000000",
        "log_queries_not_using_indexes": "OFF",
        "min_examined_row_limit": "0",
    }
    return {
        "available": True,
        "source": "FILE",
        "target_database": "tpcc",
        "entries": entries,
        "entry_count": len(entries),
        "target_entries": entries,
        "target_entry_count": len(entries),
        "matched": bool(entries),
        "variables_at_injection_start": variables,
        "variables_at_injection_end": variables,
    }


def _window(slow_sql, qps=10.0, tps=5.0):
    db_stats = {
        key: {"first": 0, "last": 0, "avg": 0, "max": 0}
        for key in (
            "Slow_queries",
            "Innodb_row_lock_time",
            "Innodb_row_lock_waits",
            "Threads_connected",
            "Threads_running",
        )
    }
    return {
        "summary_flat": {},
        "summary": {
            "db_metrics": db_stats,
            "workload": {"qps": {"avg": qps}, "tps": {"avg": tps}},
            "performance_schema_slow_sql": slow_sql,
        },
    }


def _raw_trace(executions, p95_ms, threshold_ms):
    return {
        "tasks": {
            "raw": {
                "metrics": {
                    "actions": [{
                        "kind": "raw_sql_workload",
                        "result": {
                            "executions": executions,
                            "error_count": 0,
                            "slow_threshold_sec": threshold_ms / 1000.0,
                            "slow_threshold_ms": threshold_ms,
                            "latency_ms": {"min": 1.0, "p50": 10.0, "p95": p95_ms, "max": p95_ms},
                        },
                    }]
                }
            }
        }
    }


if __name__ == "__main__":
    unittest.main()
