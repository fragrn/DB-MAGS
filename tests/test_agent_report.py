from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.report import write_report
from agent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExecutableTaskDAG,
    ExecutionTrace,
    ExperimentRequest,
    RunResult,
    TaskResult,
)


class AgentReportTests(unittest.TestCase):
    def _result(self, execution_trace) -> RunResult:
        return RunResult(
            run_id="run1",
            request=ExperimentRequest(
                target_anomaly="traffic",
                target_database="tpcc_10W",
                target_path=["traffic_surge", "slow_query"],
                injected_nodes=["traffic_surge"],
            ),
            snapshot=EnvironmentSnapshot(database="tpcc_10W"),
            dag=ExecutableTaskDAG(),
            evaluation=EvaluationResult(
                success=False,
                final_score=0.5,
                reason="not hit",
                baseline_metrics={"db_metrics": {"Threads_connected": "1"}},
                after_metrics={"db_metrics": {"Threads_connected": "10"}},
            ),
            execution_trace=execution_trace,
            rounds=1,
        )

    def test_write_report_accepts_dict_execution_trace(self):
        trace = {
            "cleanup_status": "completed",
            "cleanup_errors": [],
            "tasks": {
                "traffic_surge_1": {
                    "status": "completed",
                    "start_time": "2026-06-08T06:55:31+00:00",
                    "end_time": "2026-06-08T06:57:13+00:00",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.md"
            write_report(self._result(trace), path)
            text = path.read_text()
        self.assertIn("**Cleanup Status**: completed", text)
        self.assertIn("| `traffic_surge_1` | completed | 2026-06-08T06:55:31 | 2026-06-08T06:57:13 |", text)
        self.assertIn("Status: completed", text)

    def test_write_report_accepts_dataclass_execution_trace(self):
        trace = ExecutionTrace(
            tasks={
                "traffic_surge_1": TaskResult(
                    task_id="traffic_surge_1",
                    status="completed",
                    start_time="2026-06-08T06:55:31+00:00",
                    end_time="2026-06-08T06:57:13+00:00",
                )
            },
            cleanup_status="partial_failure",
            cleanup_errors=["cleanup failed"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.md"
            write_report(self._result(trace), path)
            text = path.read_text()
        self.assertIn("**Cleanup Status**: partial_failure", text)
        self.assertIn("- cleanup failed", text)
        self.assertIn("Status: partial_failure", text)

    def test_write_report_includes_workload_trace(self):
        result = self._result({"cleanup_status": "completed", "tasks": {}})
        result.workload_trace = {
            "config": {"runner": "benchbase", "benchmark": "tpcc", "database": "tpcc_10W"},
            "status": {"pid": 1234, "running": False, "exit_code": 0},
            "samples": [
                {"phase": "baseline"},
                {"phase": "injection"},
                {"phase": "injection"},
                {"phase": "recovery"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.md"
            write_report(result, path)
            text = path.read_text()
        self.assertIn("## 11. Background Workload", text)
        self.assertIn("- **Runner**: benchbase", text)
        self.assertIn("| injection | 2 |", text)

    def test_write_report_includes_reflection_comparison(self):
        result = self._result({"cleanup_status": "completed", "tasks": {}})
        with tempfile.TemporaryDirectory() as tmpdir:
            result.output_dir = tmpdir
            (Path(tmpdir) / "reflection_comparison.json").write_text(
                json.dumps({
                    "available": True,
                    "score_delta": 0.25,
                    "qps": {"injection": {"before_avg": 100.0, "after_avg": 70.0, "delta": -30.0}},
                    "query_latency": {
                        "injection": {
                            "overall": {
                                "before": {"avg_latency_ms": 10.0, "p95_latency_ms": 30.0, "max_latency_ms": 50.0},
                                "after": {"avg_latency_ms": 20.0, "p95_latency_ms": 60.0, "max_latency_ms": 90.0},
                            }
                        }
                    },
                }),
                encoding="utf-8",
            )
            path = Path(tmpdir) / "report.md"
            write_report(result, path)
            text = path.read_text()
        self.assertIn("## Reflection Comparison", text)
        self.assertIn("Injection QPS Avg", text)


if __name__ == "__main__":
    unittest.main()
