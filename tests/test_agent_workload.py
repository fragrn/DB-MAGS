from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.runtime import DBMAGSRuntime
from agent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExecutableTaskDAG,
    ExperimentRequest,
    SafetyResult,
)
from agent.workload import BenchBaseWorkloadRunner, MetricsCollector, normalize_workload_config


class FakeProcess:
    pid = 1234

    def __init__(self):
        self._exit = None
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 0

    def wait(self, timeout=None):
        self._exit = 0
        return 0

    def kill(self):
        self._exit = -9


class FakeRunner:
    def __init__(self, events, running=True):
        self.events = events
        self.running = running

    def start(self):
        self.events.append("start_workload")
        return {"event": "start_workload", "pid": 1}

    def stop(self):
        self.events.append("stop_workload")
        return {"event": "stop_workload", "exit_code": 0}

    def status(self):
        return {"pid": 1, "running": self.running, "exit_code": None if self.running else 0}


class StopAfterStartRunner(FakeRunner):
    def __init__(self, events):
        super().__init__(events, running=True)
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        running = self.status_calls == 1
        return {"pid": 1, "running": running, "exit_code": None if running else 0}


class FakeCollector:
    def __init__(self, events):
        self.events = events

    def collect_window(self, phase, duration_sec, interval_sec):
        self.events.append(f"collect_{phase}")
        count = max(1, int(duration_sec / interval_sec)) if interval_sec else 1
        return {
            "phase": phase,
            "duration_sec": duration_sec,
            "sample_interval_sec": interval_sec,
            "sample_count": count,
            "samples": [{"phase": phase, "index": i} for i in range(count)],
            "summary": {"flat": {"qps": 10.0, "Threads_connected": 2.0}},
            "summary_flat": {"qps": 10.0, "Threads_connected": 2.0},
            "db_metrics": {},
            "workload": {"qps": {"avg": 10.0}, "tps": {"avg": 5.0}},
            "os_metrics": {},
        }


class AgentWorkloadTests(unittest.TestCase):
    def test_request_parses_workload_config_and_defaults_to_disabled(self):
        default_req = ExperimentRequest.from_dict({"target_database": "db1"})
        self.assertEqual(default_req.workload, {})

        req = ExperimentRequest.from_dict(
            {
                "target_database": "tpcc_10W",
                "workload": {
                    "enabled": True,
                    "runner": "benchbase",
                    "benchmark": "tpcc",
                    "baseline_sec": 30,
                    "sample_interval_sec": 5,
                },
            }
        )
        self.assertTrue(req.workload["enabled"])
        self.assertEqual(req.workload["benchmark"], "tpcc")

    def test_metrics_collector_collects_expected_injection_sample_count(self):
        collector = MetricsCollector(RuntimeConfig(), "tpcc")
        collector.sample_once = lambda phase, index, interval: {  # type: ignore[method-assign]
            "phase": phase,
            "index": index,
            "db_metrics": {"Threads_connected": index},
            "workload": {"qps": 10},
            "os_metrics": {"cpu_usage": {"usage_ratio": 0.1}},
        }
        window = collector.collect_window("injection", duration_sec=60, interval_sec=5)
        self.assertEqual(window["sample_count"], 12)
        self.assertEqual(len(window["samples"]), 12)

    def test_metrics_collector_raises_when_workload_exits_mid_window(self):
        runner = FakeRunner([], running=True)
        collector = MetricsCollector(RuntimeConfig(), "tpcc", runner=runner)  # type: ignore[arg-type]

        def sample_once(phase, index, interval):
            return {
                "phase": phase,
                "index": index,
                "db_metrics": {"Threads_connected": index},
                "workload": {"qps": 10},
                "os_metrics": {"cpu_usage": {"usage_ratio": 0.1}},
                "workload_status": {"running": index == 0, "exit_code": 0},
            }

        collector.sample_once = sample_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "Background workload exited before Phase 10"):
            collector.collect_window("injection", duration_sec=10, interval_sec=5)

    def test_normalize_workload_config_preserves_explicit_duration(self):
        cfg = normalize_workload_config({"enabled": True, "duration_sec": 600}, "tpcc")
        self.assertEqual(cfg["duration_sec"], 600.0)

    def test_benchbase_runner_start_stop_with_mock_subprocess(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "tpcc.xml"
            jar_path = root / "benchbase.jar"
            config_path.write_text(
                """<?xml version="1.0"?><parameters><terminals>1</terminals><works><work><time>60</time></work></works></parameters>"""
            )
            jar_path.write_text("")
            process = FakeProcess()
            with patch("agent.workload.subprocess.Popen", return_value=process) as popen:
                runner = BenchBaseWorkloadRunner(
                    RuntimeConfig(),
                    {
                        "enabled": True,
                        "runner": "benchbase",
                        "benchmark": "tpcc",
                        "database": "tpcc",
                        "config_path": str(config_path),
                        "jar_path": str(jar_path),
                        "java_bin": "java",
                        "duration_sec": 30,
                    },
                    root,
                )
                start = runner.start()
                stop = runner.stop()
        self.assertEqual(start["pid"], 1234)
        self.assertEqual(stop["exit_code"], 0)
        self.assertTrue(process.terminated)
        self.assertTrue(popen.called)

    def test_benchbase_runner_writes_explicit_duration_to_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "tpcc.xml"
            jar_path = root / "benchbase.jar"
            config_path.write_text(
                """<?xml version="1.0"?><parameters><terminals>1</terminals><works><work><time>60</time></work></works></parameters>"""
            )
            jar_path.write_text("")
            runner = BenchBaseWorkloadRunner(
                RuntimeConfig(),
                {
                    "enabled": True,
                    "runner": "benchbase",
                    "benchmark": "tpcc",
                    "database": "tpcc",
                    "config_path": str(config_path),
                    "jar_path": str(jar_path),
                    "duration_sec": 600,
                },
                root,
            )
            runtime_config = runner._materialize_config()
            text = runtime_config.read_text()
        self.assertIn("<time>600</time>", text)

    def test_runtime_workload_enabled_phase_order_and_stop(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            max_retry_rounds=1,
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "warmup_sec": 0,
                "baseline_sec": 10,
                "injection_observe_sec": 60,
                "recovery_sec": 10,
                "sample_interval_sec": 5,
            },
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=False))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._make_metrics_collector = lambda request, runner: FakeCollector(events)  # type: ignore[method-assign]

        def inspect_named(request, round_no, round_dir, filename):
            events.append(filename)
            return EnvironmentSnapshot(database=request.target_database, db_metrics={"max_connections": 100})

        runtime._inspect_named = inspect_named  # type: ignore[method-assign]
        runtime._plan = lambda request, snapshot, round_no, round_dir, latest_reflection=None: (  # type: ignore[method-assign]
            events.append("plan") or ExecutableTaskDAG(),
            snapshot,
            [],
        )
        runtime._safety_check = lambda dag, snapshot, round_dir: events.append("safety") or SafetyResult()  # type: ignore[method-assign]
        runtime._execute = lambda dag, round_no, round_dir: events.append("execute") or {"tasks": {}}  # type: ignore[method-assign]
        runtime._evaluate = lambda **kwargs: events.append("evaluate") or EvaluationResult(success=True, final_score=1.0)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runtime.run(req, output_root=tmpdir)
            run_dirs = list(Path(tmpdir).iterdir())
            round_dir = run_dirs[0] / "round_1"
            self.assertTrue((round_dir / "workload_trace.json").exists())
            self.assertTrue((round_dir / "injection_metrics.json").exists())

        self.assertTrue(result.evaluation.success)
        self.assertEqual(events[0], "static_snapshot.json")
        self.assertIn("runtime_snapshot.json", events)
        self.assertIn("collect_baseline", events)
        self.assertIn("collect_injection", events)
        self.assertIn("collect_recovery", events)
        self.assertEqual(events[-1], "stop_workload")

    def test_runtime_raises_if_workload_exits_before_phase10_and_stops_runner(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            max_retry_rounds=1,
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "warmup_sec": 0,
                "baseline_sec": 10,
                "injection_observe_sec": 60,
                "recovery_sec": 10,
                "sample_interval_sec": 5,
            },
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=False))
        runtime._make_workload_runner = lambda request, round_dir: StopAfterStartRunner(events)  # type: ignore[method-assign]
        runtime._make_metrics_collector = lambda request, runner: FakeCollector(events)  # type: ignore[method-assign]
        runtime._inspect_named = lambda request, round_no, round_dir, filename: (  # type: ignore[method-assign]
            events.append(filename) or EnvironmentSnapshot(database=request.target_database)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Background workload exited before Phase 10"):
                runtime.run(req, output_root=tmpdir)
            run_dirs = list(Path(tmpdir).iterdir())
            round_dir = run_dirs[0] / "round_1"
            trace_text = (round_dir / "workload_trace.json").read_text()

        self.assertIn("stop_workload", events)
        self.assertIn("workload_exited_before_phase10", trace_text)

    def test_runtime_raises_if_anomaly_execution_fails_before_evaluation(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            max_retry_rounds=1,
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "warmup_sec": 0,
                "baseline_sec": 10,
                "injection_observe_sec": 10,
                "recovery_sec": 10,
                "sample_interval_sec": 5,
            },
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=False))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._make_metrics_collector = lambda request, runner: FakeCollector(events)  # type: ignore[method-assign]
        runtime._inspect_named = lambda request, round_no, round_dir, filename: (  # type: ignore[method-assign]
            events.append(filename) or EnvironmentSnapshot(database=request.target_database)
        )
        runtime._plan = lambda request, snapshot, round_no, round_dir, latest_reflection=None: (  # type: ignore[method-assign]
            events.append("plan") or ExecutableTaskDAG(),
            snapshot,
            [],
        )
        runtime._safety_check = lambda dag, snapshot, round_dir: events.append("safety") or SafetyResult()  # type: ignore[method-assign]
        runtime._execute = lambda dag, round_no, round_dir: events.append("execute") or {  # type: ignore[method-assign]
            "tasks": {
                "traffic": {
                    "status": "failed",
                    "stderr": "'str' object has no attribute 'get'",
                    "errors": ["'str' object has no attribute 'get'"],
                }
            },
            "cleanup_status": "completed",
            "cleanup_errors": [],
        }
        runtime._evaluate = lambda **kwargs: events.append("evaluate") or EvaluationResult(success=True)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Anomaly injection failed before evaluation"):
                runtime.run(req, output_root=tmpdir)
            run_dirs = list(Path(tmpdir).iterdir())
            round_dir = run_dirs[0] / "round_1"
            trace_text = (round_dir / "workload_trace.json").read_text()

        self.assertIn("collect_injection", events)
        self.assertNotIn("collect_recovery", events)
        self.assertNotIn("evaluate", events)
        self.assertIn("stop_workload", events)
        self.assertIn("execution_failed_before_evaluation", trace_text)


if __name__ == "__main__":
    unittest.main()
