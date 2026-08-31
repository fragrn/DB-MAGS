from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.executor import Executor
from agent.runtime import DBMAGSRuntime
from agent.safety import SafetyChecker
from agent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExecutableTaskDAG,
    ExperimentRequest,
    NodeResult,
    SafetyResult,
    TaskSpec,
    to_jsonable,
)
from agent.planner import PlannerFallbackError
from agent.planner import GlobalPlanner
from agent.reflection import ReflectionFallbackError
from agent.workload import BenchBaseWorkloadRunner, MetricsCollector, normalize_workload_config
from agent.tools import (
    build_traffic_task,
    canonicalize_task_dag_runtime_paths,
    canonicalize_resource_chaosblade_action,
    get_benchbase_workload_defaults,
)


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


class FakeFailingProcess(FakeProcess):
    def wait(self, timeout=None):
        self._exit = 2
        return 2


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
    def test_writes_round_observation_summary_and_reflection_comparison(self):
        runtime = DBMAGSRuntime(RuntimeConfig())

        def window(qps, avg_ms, max_ms):
            return {
                "duration_sec": 1,
                "sample_count": 1,
                "summary": {
                    "workload": {"qps": {"avg": qps, "min": qps, "max": qps}, "tps": {"avg": 1.0}},
                    "query_latency_top10": [{
                        "digest": "d1",
                        "digest_text": "SELECT * FROM order_line",
                        "execution_count": 2,
                        "avg_latency_ms": avg_ms,
                        "median_latency_ms": avg_ms,
                        "p95_latency_ms": max_ms,
                        "max_latency_ms": max_ms,
                        "total_latency_ms": avg_ms * 2,
                    }],
                    "query_latency_overall": {
                        "count": 2,
                        "avg_latency_ms": avg_ms,
                        "median_latency_ms": avg_ms,
                        "p95_latency_ms": max_ms,
                        "max_latency_ms": max_ms,
                        "total_latency_ms": avg_ms * 2,
                    },
                },
                "summary_flat": {"qps": qps},
            }

        trace = {
            "tasks": {
                "slow_sql_task": {
                    "metrics": {
                        "actions": [{
                            "result": {
                                "kind": "raw_sql_workload",
                                "executions": 8,
                                "avg_ms": 1100.0,
                                "median_ms": 1000.0,
                                "p95_ms": 2000.0,
                                "max_ms": 2200.0,
                                "top10_slowest_ms": [2200.0, 2000.0],
                                "above_long_query_time_count": 2,
                            }
                        }]
                    }
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            round1 = output_dir / "round_1"
            round2 = output_dir / "round_2"
            round1.mkdir()
            round2.mkdir()
            ev1 = EvaluationResult(final_score=0.4, success=False, node_results={"slow_query": NodeResult("slow_query", False, 0.0)})
            ev2 = EvaluationResult(final_score=0.7, success=False, node_results={"slow_query": NodeResult("slow_query", True, 1.0)})
            runtime._write_round_observation_summary(
                round1,
                1,
                window(100.0, 10.0, 50.0),
                window(80.0, 20.0, 100.0),
                window(95.0, 12.0, 60.0),
                trace,
                {"available": True, "target_entry_count": 0, "target_entries": []},
                ev1,
            )
            runtime._write_round_observation_summary(
                round2,
                2,
                window(100.0, 10.0, 50.0),
                window(60.0, 40.0, 200.0),
                window(90.0, 14.0, 70.0),
                trace,
                {"available": True, "target_entry_count": 1, "target_entries": [{"sql": "SELECT 1"}]},
                ev2,
            )
            comparison = runtime._write_reflection_comparison(output_dir)
            round_summary = json.loads((round2 / "round_observation_summary.json").read_text())

        self.assertEqual(round_summary["phases"]["injection"]["qps"]["avg"], 60.0)
        self.assertEqual(round_summary["phases"]["injection"]["query_latency_top10"][0]["max_latency_ms"], 200.0)
        self.assertEqual(round_summary["raw_sql_workload"][0]["p95_ms"], 2000.0)
        self.assertTrue(comparison["available"])
        self.assertAlmostEqual(comparison["score_delta"], 0.3)
        self.assertEqual(comparison["qps"]["injection"]["delta"], -20.0)
        self.assertEqual(comparison["node_hit_changes"]["slow_query"], {"before": False, "after": True})

    def test_slow_log_capture_restores_configuration_when_collection_fails(self):
        class FailingProbe:
            restored = False

            def collect(self, marker, target_database=None):
                raise RuntimeError("slow log read failed")

            def restore(self, marker):
                self.restored = True
                return {"restored": True, "changed_by_probe": True, "error": ""}

        runtime = DBMAGSRuntime(RuntimeConfig())
        probe = FailingProbe()
        marker = {
            "variables_at_injection_start": {
                "long_query_time": "10.000000",
                "log_queries_not_using_indexes": "OFF",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = runtime._finish_slow_log_capture(
                probe, marker, "tpcc", Path(tmpdir)  # type: ignore[arg-type]
            )
            artifact = json.loads((Path(tmpdir) / "slow_log_evidence.json").read_text())
        self.assertTrue(probe.restored)
        self.assertFalse(evidence["available"])
        self.assertEqual(artifact["restore"]["restored"], True)

    def test_default_runtime_executable_paths_are_absolute_and_exist(self):
        config = RuntimeConfig()
        for value in (config.benchbase_jar_path, config.chaosblade_path):
            path = Path(value)
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_file(), value)

    def test_canonicalizes_llm_guessed_benchbase_and_chaosblade_paths(self):
        config = RuntimeConfig()
        workload = normalize_workload_config({"enabled": True, "benchmark": "tpcc"}, "tpcc_10W")
        dag = {
            "tasks": {
                "traffic": {
                    "metadata": {"root_cause": "traffic_surge"},
                    "actions": [{
                        "kind": "benchbase_burst_command",
                        "command": ["java", "-jar", "wrong.jar", "-b", "tpcc", "-c", "wrong.xml"],
                    }],
                },
                "cpu": {
                    "metadata": {"root_cause": "resource_cpu"},
                    "actions": [{
                        "kind": "raw_command",
                        "command": ["blade", "create", "cpu", "load", "--uid", "cpu-1"],
                        "cleanup_command": ["blade", "destroy", "cpu-1"],
                    }],
                },
            }
        }

        normalized = canonicalize_task_dag_runtime_paths(config, dag, expected_workload=workload)
        traffic_command = normalized["tasks"]["traffic"]["actions"][0]["command"]
        resource_action = normalized["tasks"]["cpu"]["actions"][0]

        self.assertEqual(traffic_command[0], workload["java_bin"])
        self.assertEqual(traffic_command[traffic_command.index("-jar") + 1], config.benchbase_jar_path)
        self.assertEqual(traffic_command[traffic_command.index("-c") + 1], workload["config_path"])
        self.assertEqual(resource_action["command"][0], config.chaosblade_path)
        self.assertEqual(resource_action["command"][3], "fullload")
        self.assertEqual(resource_action["command"][resource_action["command"].index("--timeout") + 1], "30")
        self.assertEqual(resource_action["cleanup_command"][0], config.chaosblade_path)
        resource_uid = resource_action["command"][resource_action["command"].index("--uid") + 1]
        self.assertNotEqual(resource_uid, "cpu-1")
        self.assertEqual(resource_action["cleanup_command"], [config.chaosblade_path, "destroy", resource_uid])
        self.assertEqual(resource_action["chaosblade_original_uid"], "cpu-1")
        self.assertEqual(dag["tasks"]["cpu"]["actions"][0]["command"][0], "blade")

    def test_canonicalizes_legacy_resource_chaosblade_commands(self):
        config = RuntimeConfig(chaosblade_path="/opt/blade")

        cpu = canonicalize_resource_chaosblade_action(
            config,
            {
                "kind": "raw_command",
                "duration_sec": 7,
                "command": ["/bad/blade", "create", "cpu", "load", "--cpu-percent", "81", "--duration", "7", "--process-name", "mysqld", "--uid", "cpu_uid"],
                "cleanup_command": ["/bad/blade", "destroy", "cpu_uid"],
            },
            root_cause="resource_cpu",
        )
        cpu_uid = cpu["command"][cpu["command"].index("--uid") + 1]
        self.assertEqual(cpu["command"][:-1], ["/opt/blade", "create", "cpu", "fullload", "--cpu-percent", "81", "--timeout", "7", "--uid"])
        self.assertNotEqual(cpu_uid, "cpu_uid")
        self.assertEqual(cpu["cleanup_command"], ["/opt/blade", "destroy", cpu_uid])
        self.assertEqual(cpu["chaosblade_original_uid"], "cpu_uid")

        mem = canonicalize_resource_chaosblade_action(
            config,
            {
                "kind": "raw_command",
                "duration_sec": 8,
                "command": ["/bad/blade", "create", "mem", "--mem-percent", "72", "--duration", "8", "--uid=mem_uid"],
                "cleanup_command": ["/bad/blade", "destroy", "mem_uid"],
            },
            root_cause="resource_memory",
        )
        mem_uid = mem["command"][mem["command"].index("--uid") + 1]
        self.assertEqual(mem["command"][:-1], ["/opt/blade", "create", "mem", "load", "--mode", "ram", "--mem-percent", "72", "--timeout", "8", "--uid"])
        self.assertNotEqual(mem_uid, "mem_uid")
        self.assertEqual(mem["cleanup_command"], ["/opt/blade", "destroy", mem_uid])
        self.assertEqual(mem["chaosblade_original_uid"], "mem_uid")

        disk = canonicalize_resource_chaosblade_action(
            config,
            {
                "kind": "raw_command",
                "duration_sec": 9,
                "command": ["/bad/blade", "create", "disk", "fill", "--path", "/tmp/dbmags", "--size", "200M", "--read-bps", "100M", "--write-bps", "100M", "--uid", "io_uid"],
                "cleanup_command": ["/bad/blade", "destroy", "io_uid"],
            },
            root_cause="resource_io",
        )
        disk_uid = disk["command"][disk["command"].index("--uid") + 1]
        self.assertEqual(disk["command"][:-1], ["/opt/blade", "create", "disk", "burn", "--read", "--write", "--path", "/tmp/dbmags", "--size", "200M", "--timeout", "9", "--uid"])
        self.assertNotEqual(disk_uid, "io_uid")
        self.assertEqual(disk["cleanup_command"], ["/opt/blade", "destroy", disk_uid])
        self.assertEqual(disk["chaosblade_original_uid"], "io_uid")

        network = canonicalize_resource_chaosblade_action(
            config,
            {
                "kind": "raw_command",
                "duration_sec": 10,
                "command": ["/bad/blade", "create", "network", "delay", "--time", "200", "--interface", "lo0", "--uid", "net_uid"],
                "cleanup_command": ["/bad/blade", "destroy", "net_uid"],
            },
            root_cause="network_latency",
        )
        net_uid = network["command"][network["command"].index("--uid") + 1]
        self.assertEqual(network["command"][:-1], ["/opt/blade", "create", "network", "drop", "--destination-port", "3306", "--network-traffic", "out", "--timeout", "10", "--uid"])
        self.assertNotEqual(net_uid, "net_uid")
        self.assertEqual(network["cleanup_command"], ["/opt/blade", "destroy", net_uid])

    def test_planner_normalizes_common_new_node_root_aliases(self):
        specs = [
            {"metadata": {"root_cause": "redo_log_flush_stall"}},
            {"metadata": {"root_cause": "metadata_lock_wait"}},
        ]
        GlobalPlanner._normalize_task_spec_root_aliases(
            specs,
            ExperimentRequest(
                target_path=["redo_log_pressure", "redo_log_flush_stall"],
                injected_nodes=["redo_log_pressure", "metadata_lock"],
            ),
        )
        self.assertEqual(specs[0]["metadata"]["root_cause"], "redo_log_pressure")
        self.assertEqual(specs[1]["metadata"]["root_cause"], "metadata_lock")

    def test_canonicalize_task_dag_ignores_non_object_actions(self):
        config = RuntimeConfig(chaosblade_path="/opt/blade")
        dag = {
            "tasks": {
                "bad": {
                    "metadata": {"root_cause": "long_tx"},
                    "actions": ["BEGIN", {"kind": "raw_transaction_script", "duration_sec": 5}],
                }
            }
        }

        normalized = canonicalize_task_dag_runtime_paths(config, dag)

        self.assertEqual(normalized["tasks"]["bad"]["actions"][0], "BEGIN")
        self.assertEqual(normalized["tasks"]["bad"]["actions"][1]["kind"], "raw_transaction_script")

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

    def test_normalize_workload_config_supports_tpch_and_requires_tatp_config(self):
        tpch = normalize_workload_config({"enabled": True, "benchmark": "tpch"}, "tpch_1SF")
        self.assertEqual(tpch["benchmark"], "tpch")
        self.assertIn("local_tpch_1SF_config.xml", tpch["config_path"])
        with self.assertRaisesRegex(ValueError, "config_path is required"):
            normalize_workload_config({"enabled": True, "benchmark": "tatp"}, "tatp")

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
        self.assertTrue(Path(start["runtime_config_path"]).is_absolute())
        command = popen.call_args.args[0]
        self.assertTrue(Path(command[command.index("-c") + 1]).is_absolute())
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

    def test_benchbase_workload_defaults_are_not_candidate_templates(self):
        result = get_benchbase_workload_defaults(
            "tpcc",
            config_path=".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
            database="tpcc_10W",
            terminals=16,
            duration_sec=60,
        )
        self.assertEqual(result["benchmark"], "tpcc")
        self.assertEqual(result["default_transaction_mix"]["NewOrder"], 45.0)
        self.assertEqual(result["default_transaction_mix"]["Payment"], 43.0)
        self.assertEqual(result["default_terminals"], 16)
        self.assertIn("forbidden_fields", result["constraints"])
        self.assertNotIn("candidates", result)
        self.assertNotIn("preferred_template", result)
        self.assertNotIn("task_specs", result)

    def test_benchbase_workload_defaults_support_tpch_and_tatp(self):
        tpch = get_benchbase_workload_defaults("tpch")
        tatp = get_benchbase_workload_defaults("tatp")
        self.assertEqual(len(tpch["legal_transaction_types"]), 22)
        self.assertIn("Q22", tpch["legal_transaction_types"])
        self.assertIn("GetSubscriberData", tatp["legal_transaction_types"])
        self.assertEqual(tatp["benchmark"], "tatp")

    def test_build_traffic_task_requires_profile_and_rejects_sql(self):
        profile = _traffic_profile()
        task = build_traffic_task(RuntimeConfig(), profile=profile, task_id="traffic")
        self.assertEqual(task["actions"][0]["kind"], "benchbase_burst")
        self.assertEqual(task["actions"][0]["profile"], task["metadata"]["traffic_surge_profile"])
        bad = dict(profile)
        bad["sql"] = "SELECT 1"
        with self.assertRaisesRegex(ValueError, "forbidden fields"):
            build_traffic_task(RuntimeConfig(), profile=bad, task_id="traffic")

    def test_build_traffic_task_accepts_tpch_and_tatp_profiles(self):
        tpch_task = build_traffic_task(RuntimeConfig(), profile=_traffic_profile_for("tpch"), task_id="tpch_traffic")
        tatp_task = build_traffic_task(RuntimeConfig(), profile=_traffic_profile_for("tatp"), task_id="tatp_traffic")
        self.assertEqual(tpch_task["actions"][0]["benchmark"], "tpch")
        self.assertEqual(tatp_task["actions"][0]["benchmark"], "tatp")
        bad = _traffic_profile_for("tpch")
        bad["transaction_mix"]["NewOrder"] = 1
        with self.assertRaisesRegex(ValueError, "unknown transaction types"):
            build_traffic_task(RuntimeConfig(), profile=bad, task_id="bad")

    def test_executor_materializes_benchbase_burst_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "tpcc.xml"
            source.write_text(
                """<?xml version="1.0"?><parameters><dbtype>mysql</dbtype><url>jdbc:mysql://127.0.0.1/old_db?x=1</url><terminals>1</terminals><works><work><time>60</time><rate>10</rate><weights>45,43,4,4,4</weights></work></works></parameters>"""
            )
            profile = _traffic_profile(config_path=str(source), database="tpcc_10W")
            executor = Executor(RuntimeConfig(), round_dir=str(root))
            runtime_config = executor._materialize_benchbase_burst_config(profile, "traffic")
            text = runtime_config.read_text()
        self.assertIn("<time>15</time>", text)
        self.assertIn("<terminals>8</terminals>", text)
        self.assertIn("<rate>120.0</rate>", text)
        self.assertIn("<weights>50,45,1,2,2</weights>", text)
        self.assertIn("jdbc:mysql://127.0.0.1/tpcc_10W?x=1", text)

    def test_executor_materializes_tpch_and_tatp_weights(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tpch_source = root / "tpch.xml"
            tatp_source = root / "tatp.xml"
            tpch_source.write_text(_benchbase_xml("tpch", [f"Q{i}" for i in range(1, 23)], weights=",".join(["1"] * 22)))
            tatp_names = [
                "DeleteCallForwarding",
                "GetAccessData",
                "GetNewDestination",
                "GetSubscriberData",
                "InsertCallForwarding",
                "UpdateLocation",
                "UpdateSubscriberData",
            ]
            tatp_source.write_text(_benchbase_xml("tatp", tatp_names, weights="2,35,10,35,2,14,2"))
            executor = Executor(RuntimeConfig(), round_dir=str(root))
            tpch_xml = executor._materialize_benchbase_burst_config(
                _traffic_profile_for("tpch", config_path=str(tpch_source), database="tpch_1SF"),
                "tpch_traffic",
            ).read_text()
            tatp_xml = executor._materialize_benchbase_burst_config(
                _traffic_profile_for("tatp", config_path=str(tatp_source), database="tatp"),
                "tatp_traffic",
            ).read_text()
        self.assertIn("<weights>" + ",".join(["1"] * 22) + "</weights>", tpch_xml)
        self.assertIn("<weights>2,35,10,35,2,14,2</weights>", tatp_xml)
        self.assertIn("jdbc:mysql://127.0.0.1/tpch_1SF?x=1", tpch_xml)
        self.assertIn("jdbc:mysql://127.0.0.1/tatp?x=1", tatp_xml)

    def test_executor_runs_raw_command_and_fails_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = Executor(RuntimeConfig(), round_dir=tmpdir)
            ok = executor._run_action(
                "raw_command",
                {"kind": "raw_command", "command": [sys.executable, "-c", "print('raw-ok')"], "duration_sec": 1},
                "raw_ok",
            )
            self.assertEqual(ok["exit_code"], 0)
            self.assertIn("raw-ok", ok["stdout_tail"])
            with self.assertRaisesRegex(RuntimeError, "exited with 3"):
                executor._run_action(
                    "raw_command",
                    {"kind": "raw_command", "command": [sys.executable, "-c", "import sys; sys.exit(3)"], "duration_sec": 1},
                    "raw_bad",
                )

    def test_executor_holds_resource_chaosblade_until_duration_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = Executor(RuntimeConfig(chaosblade_path="/opt/blade"), round_dir=tmpdir)
            sleeps: list[float] = []
            cleanup_calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                cleanup_calls.append(list(command))
                class Result:
                    returncode = 0
                    stdout = "destroyed"
                    stderr = ""
                return Result()

            with patch("agent.executor.subprocess.Popen", return_value=FakeProcess()), \
                    patch("agent.executor.subprocess.run", side_effect=fake_run), \
                    patch("agent.executor.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                result = executor._run_action(
                    "raw_command",
                    {
                        "kind": "raw_command",
                        "command": ["/opt/blade", "create", "cpu", "fullload", "--cpu-percent", "80", "--timeout", "2", "--uid", "cpu_uid"],
                        "duration_sec": 2,
                        "cleanup_command": ["/opt/blade", "destroy", "cpu_uid"],
                    },
                    "resource_cpu",
                )

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(cleanup_calls, [["/opt/blade", "destroy", "cpu_uid"]])
            self.assertTrue(any(seconds > 0 for seconds in sleeps), sleeps)
            self.assertGreater(result["hold_after_success_sec"], 0)

    def test_executor_cleans_up_resource_chaosblade_after_create_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = Executor(RuntimeConfig(chaosblade_path="/opt/blade"), round_dir=tmpdir)
            cleanup_calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                cleanup_calls.append(list(command))
                class Result:
                    returncode = 0
                    stdout = "destroyed"
                    stderr = ""
                return Result()

            with patch("agent.executor.subprocess.Popen", return_value=FakeFailingProcess()), \
                    patch("agent.executor.subprocess.run", side_effect=fake_run), \
                    patch("agent.executor.time.sleep") as sleep_mock:
                with self.assertRaisesRegex(RuntimeError, "exited with 2"):
                    executor._run_action(
                        "raw_command",
                        {
                            "kind": "raw_command",
                            "command": ["/opt/blade", "create", "cpu", "fullload", "--cpu-percent", "80", "--timeout", "2", "--uid", "cpu_uid"],
                            "duration_sec": 2,
                            "cleanup_command": ["/opt/blade", "destroy", "cpu_uid"],
                        },
                        "resource_cpu",
                    )

            self.assertEqual(cleanup_calls, [["/opt/blade", "destroy", "cpu_uid"]])
            sleep_mock.assert_not_called()

    def test_deadlock_storm_treats_mysql_1213_as_expected_error(self):
        class DeadlockError(Exception):
            def __init__(self):
                super().__init__(1213, "Deadlock found when trying to get lock; try restarting transaction")

        class Cursor:
            def execute(self, sql, params=None):
                raise DeadlockError()

            def close(self):
                return None

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = Executor(RuntimeConfig(), round_dir=tmpdir)
            with patch("pymysql.connect", return_value=Connection()):
                result = executor._run_raw_transaction_script({
                    "database": "tpcc",
                    "duration_sec": 1,
                    "expected_error_codes": [1213],
                    "scripts": [{"role": "deadlock_a", "steps": [{"sql": "UPDATE t SET v = v + 1"}]}],
                })

        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["expected_error_count"], 1)
        self.assertEqual(result["expected_error_codes"], [1213])

    def test_benchbase_burst_command_materializes_requested_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.xml"
            source.write_text(
                """<?xml version="1.0"?><parameters><url>jdbc:mysql://127.0.0.1:3306/old_db</url>"
                "<terminals>1</terminals><works><work><time>60</time><rate>10</rate></work></works></parameters>"""
            )
            executor = Executor(RuntimeConfig(), round_dir=str(root / "round"))
            command = executor._materialize_benchbase_command_config(
                {
                    "database": "tpcc_10W",
                    "terminals": 12,
                    "rate": 300,
                },
                ["java", "-jar", "benchbase.jar", "-c", str(source), "--execute=true"],
                "traffic",
                15.0,
            )
            generated = Path(command[command.index("-c") + 1])
            xml = generated.read_text()

        self.assertTrue(generated.is_absolute())
        self.assertIn("<time>15</time>", xml)
        self.assertIn("<terminals>12</terminals>", xml)
        self.assertIn("<rate>300</rate>", xml)
        self.assertIn("/tpcc_10W", xml)

    def test_safety_rejects_burst_duration_outside_injection_window(self):
        task = build_traffic_task(RuntimeConfig(), profile=_traffic_profile(duration_sec=30), task_id="traffic")
        dag = {"tasks": {"traffic": task}, "edges": [], "schedule": {}}
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=60,
            injection_observe_sec=10,
        )
        self.assertFalse(result.approved)
        self.assertIn("injection_observe_sec", "; ".join(result.reasons))

    def test_safety_rejects_generic_sql_and_lock_outside_injection_window(self):
        dag = {
            "tasks": {
                "sql": {
                    "task_id": "sql",
                    "task_type": "slow_sql",
                    "actions": [{"kind": "sql_workload", "duration_sec": 20, "concurrency": 1, "sql": "SELECT 1"}],
                },
                "lock": {
                    "task_id": "lock",
                    "task_type": "lock_conflict",
                    "actions": [{"kind": "lock_conflict", "hold_sec": 20}],
                },
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=15,
            expected_workload={"benchmark": "tpcc", "database": "tpcc_10W", "config_path": "tpcc.xml"},
        )
        self.assertFalse(result.approved)
        self.assertIn("DAG required duration", "; ".join(result.reasons))

    def test_safety_rejects_workload_ramp_when_background_workload_enabled(self):
        dag = {
            "tasks": {
                "traffic": {
                    "task_id": "traffic",
                    "task_type": "traffic_surge",
                    "actions": [{"kind": "workload_ramp", "duration_sec": 10}],
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=15,
            expected_workload={"benchmark": "tpcc", "database": "tpcc_10W", "config_path": "tpcc.xml"},
        )
        self.assertFalse(result.approved)
        self.assertIn("workload_ramp", "; ".join(result.reasons))

    def test_safety_rejects_burst_benchmark_mismatch(self):
        task = build_traffic_task(RuntimeConfig(), profile=_traffic_profile_for("tpcc"), task_id="traffic")
        dag = {"tasks": {"traffic": task}, "edges": [], "schedule": {}}
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=60,
            injection_observe_sec=30,
            expected_workload={"benchmark": "tpch", "database": "tpch_1SF", "config_path": "tpch.xml"},
        )
        self.assertFalse(result.approved)
        self.assertIn("does not match background workload", "; ".join(result.reasons))

    def test_safety_rejects_invalid_benchbase_burst_command_executable(self):
        dag = {
            "tasks": {
                "traffic": {
                    "task_id": "traffic",
                    "task_type": "traffic_surge",
                    "actions": [
                        {
                            "kind": "benchbase_burst_command",
                            "benchmark": "tpcc",
                            "database": "tpcc_10W",
                            "command": [
                                ".tools/benchbase-main/target/benchbase-mysql/benchbase-mysql",
                                "-b",
                                "tpcc",
                                "-c",
                                ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
                                "-s",
                                "execute",
                            ],
                            "duration_sec": 5,
                            "terminals": 2,
                        }
                    ],
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
            expected_workload={"benchmark": "tpcc", "database": "tpcc_10W"},
        )
        self.assertFalse(result.approved)
        reasons = "; ".join(result.reasons)
        self.assertIn("must invoke java/java_bin", reasons)
        self.assertIn("java -jar benchbase.jar", reasons)

    def test_safety_allows_valid_benchbase_burst_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            java_path = root / "java"
            jar_path = root / "benchbase.jar"
            config_path = root / "tpcc.xml"
            java_path.write_text("")
            jar_path.write_text("")
            config_path.write_text("<parameters/>")
            dag = {
                "tasks": {
                    "traffic": {
                        "task_id": "traffic",
                        "task_type": "traffic_surge",
                        "actions": [
                            {
                                "kind": "benchbase_burst_command",
                                "benchmark": "tpcc",
                                "database": "tpcc_10W",
                                "command": [
                                    str(java_path),
                                    "-jar",
                                    str(jar_path),
                                    "-b",
                                    "tpcc",
                                    "-c",
                                    str(config_path),
                                    "--execute=true",
                                ],
                                "duration_sec": 5,
                                "terminals": 2,
                            }
                        ],
                    }
                },
                "edges": [],
                "schedule": {},
            }
            result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
                dag,
                current_db_metrics={"max_connections": 100, "Threads_connected": 1},
                max_duration_sec=30,
                injection_observe_sec=10,
                expected_workload={"benchmark": "tpcc", "database": "tpcc_10W"},
            )
        self.assertTrue(result.approved, result.reasons)

    def test_safety_checks_raw_actions(self):
        dag = {
            "tasks": {
                "sql": {
                    "task_id": "sql",
                    "task_type": "slow_sql",
                    "actions": [{"kind": "raw_sql_workload", "duration_sec": 5, "concurrency": 1, "sql": "DROP TABLE orders"}],
                },
                "cmd": {
                    "task_id": "cmd",
                    "task_type": "resource",
                    "actions": [{"kind": "raw_command", "duration_sec": 5, "command": ["rm", "-rf", "/tmp/nope"]}],
                },
                "burst": {
                    "task_id": "burst",
                    "task_type": "traffic_surge",
                    "actions": [
                        {
                            "kind": "benchbase_burst_command",
                            "benchmark": "tpcc",
                            "database": "tpcc_10W",
                            "command": ["java", "-jar", "benchbase.jar"],
                            "duration_sec": 20,
                            "terminals": 2,
                        }
                    ],
                },
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(max_connection_usage_ratio=1.0)).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
            expected_workload={"benchmark": "tpch", "database": "tpch_1SF"},
        )
        reasons = "; ".join(result.reasons)
        self.assertFalse(result.approved)
        self.assertIn("DROP", reasons)
        self.assertIn("rm\\s+-rf", reasons)
        self.assertIn("injection_observe_sec", reasons)
        self.assertIn("does not match background workload", reasons)

    def test_safety_allows_resource_chaosblade_raw_command(self):
        dag = {
            "tasks": {
                "resource": {
                    "task_id": "resource",
                    "task_type": "resource_cpu",
                    "actions": [
                        {
                            "kind": "raw_command",
                            "command": ["/opt/blade", "create", "cpu", "fullload", "--cpu-percent", "80", "--timeout", "5", "--uid", "cpu_uid_1"],
                            "duration_sec": 5,
                            "cleanup_command": ["/opt/blade", "destroy", "cpu_uid_1"],
                        }
                    ],
                    "metadata": {"root_cause": "resource_cpu"},
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(chaosblade_path="/opt/blade")).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
        )
        self.assertTrue(result.approved, result.reasons)

    def test_safety_rejects_resource_non_chaosblade_raw_command(self):
        dag = {
            "tasks": {
                "resource": {
                    "task_id": "resource",
                    "task_type": "resource_io",
                    "actions": [
                        {
                            "kind": "raw_command",
                            "command": ["fio", "--name", "io"],
                            "duration_sec": 5,
                            "cleanup_command": ["blade", "destroy", "io_uid_1"],
                        }
                    ],
                    "metadata": {"root_cause": "resource_io"},
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(chaosblade_path="/opt/blade")).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
        )
        self.assertFalse(result.approved)
        self.assertIn("RuntimeConfig.chaosblade_path", "; ".join(result.reasons))

    def test_safety_rejects_resource_bare_blade_command(self):
        dag = {
            "tasks": {
                "resource": {
                    "task_id": "resource",
                    "task_type": "resource_io",
                    "actions": [
                        {
                            "kind": "raw_command",
                            "command": ["blade", "create", "disk", "fill", "--path", "/tmp", "--size", "100M", "--uid", "io_uid_1"],
                            "duration_sec": 5,
                            "cleanup_command": ["blade", "destroy", "io_uid_1"],
                        }
                    ],
                    "metadata": {"root_cause": "resource_io"},
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(chaosblade_path="/opt/blade")).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
        )
        self.assertFalse(result.approved)
        reasons = "; ".join(result.reasons)
        self.assertIn("resource command must invoke RuntimeConfig.chaosblade_path", reasons)
        self.assertIn("resource cleanup_command must invoke RuntimeConfig.chaosblade_path", reasons)

    def test_safety_rejects_resource_cleanup_uid_mismatch(self):
        dag = {
            "tasks": {
                "resource": {
                    "task_id": "resource",
                    "task_type": "resource_memory",
                    "actions": [
                        {
                            "kind": "raw_command",
                            "command": ["/opt/blade", "create", "mem", "load", "--mode", "ram", "--mem-percent", "70", "--timeout", "5", "--uid=mem_uid_1"],
                            "duration_sec": 5,
                            "cleanup_command": ["/opt/blade", "destroy", "other_uid"],
                        }
                    ],
                    "metadata": {"root_cause": "resource_memory"},
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(chaosblade_path="/opt/blade")).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
        )
        self.assertFalse(result.approved)
        self.assertIn("same ChaosBlade uid", "; ".join(result.reasons))

    def test_safety_rejects_resource_non_create_command(self):
        dag = {
            "tasks": {
                "resource": {
                    "task_id": "resource",
                    "task_type": "resource_network",
                    "actions": [
                        {
                            "kind": "raw_command",
                            "command": ["/opt/blade", "status", "network", "--uid", "net_uid_1"],
                            "duration_sec": 5,
                            "cleanup_command": ["/opt/blade", "destroy", "net_uid_1"],
                        }
                    ],
                    "metadata": {"root_cause": "resource_network"},
                }
            },
            "edges": [],
            "schedule": {},
        }
        result = SafetyChecker(RuntimeConfig(chaosblade_path="/opt/blade")).check(
            dag,
            current_db_metrics={"max_connections": 100, "Threads_connected": 1},
            max_duration_sec=30,
            injection_observe_sec=10,
        )
        self.assertFalse(result.approved)
        self.assertIn("must use create", "; ".join(result.reasons))

    def test_runtime_timing_validation_rejects_short_workload_duration(self):
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            max_duration_sec=30,
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "duration_sec": 20,
                "warmup_sec": 5,
                "baseline_sec": 10,
                "injection_observe_sec": 15,
                "recovery_sec": 5,
                "sample_interval_sec": 5,
            },
        )
        runtime = DBMAGSRuntime(RuntimeConfig())
        cfg = normalize_workload_config(req.workload, req.target_database)
        with tempfile.TemporaryDirectory() as tmpdir:
            round_dir = Path(tmpdir)
            with self.assertRaisesRegex(RuntimeError, "shorter than required"):
                runtime._validate_workload_timing_before_start(req, cfg, round_dir)
            text = (round_dir / "workload_timing_validation.json").read_text()
        self.assertIn('"status": "failed"', text)

    def test_runtime_phase_validation_rejects_dag_longer_than_injection_window(self):
        req = ExperimentRequest(
            target_anomaly="slow",
            target_database="tpcc",
            target_path=["missing_index", "slow_query"],
            injected_nodes=["missing_index"],
            max_duration_sec=30,
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "config_path": "tpcc.xml",
                "injection_observe_sec": 15,
            },
        )
        dag = ExecutableTaskDAG(tasks={
            "sql": TaskSpec(
                task_id="sql",
                task_type="slow_sql",
                actions=[{"kind": "sql_workload", "duration_sec": 20, "concurrency": 1, "sql": "SELECT 1"}],
                metadata={"root_cause": "missing_index"},
            )
        })
        runtime = DBMAGSRuntime(RuntimeConfig())
        with tempfile.TemporaryDirectory() as tmpdir:
            round_dir = Path(tmpdir)
            with self.assertRaisesRegex(RuntimeError, "Timing validation failed"):
                runtime._validate_benchbase_burst_windows(req, dag, round_dir)
            text = (round_dir / "timing_validation.json").read_text()
        self.assertIn('"status": "failed"', text)

    def test_runtime_execute_uses_request_max_duration(self):
        req = ExperimentRequest(target_database="tpcc", max_duration_sec=17)
        dag = ExecutableTaskDAG()
        runtime = DBMAGSRuntime(RuntimeConfig(max_duration_sec=300))
        captured = {}

        def fake_execute_dag(task_dag, config, max_duration_sec=300, round_dir=""):
            captured["max_duration_sec"] = max_duration_sec
            return {"tasks": {}}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent.tools.execute_dag", fake_execute_dag):
                runtime._execute(dag, 1, Path(tmpdir), req)
        self.assertEqual(captured["max_duration_sec"], 17)

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
        runtime._safety_check = lambda dag, snapshot, round_dir, request=None: events.append("safety") or SafetyResult()  # type: ignore[method-assign]
        runtime._execute = lambda dag, round_no, round_dir, request: events.append("execute") or {"tasks": {}}  # type: ignore[method-assign]
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

    def test_runtime_blocks_planner_fallback_writes_artifacts_and_stops_runner(self):
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
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._make_metrics_collector = lambda request, runner: FakeCollector(events)  # type: ignore[method-assign]
        runtime._inspect_named = lambda request, round_no, round_dir, filename: (  # type: ignore[method-assign]
            events.append(filename) or EnvironmentSnapshot(database=request.target_database)
        )
        runtime.planner.plan = lambda request, snapshot, memory_items, reflection=None: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(PlannerFallbackError(
                "Planner fallback blocked after tool loop failure: timeout",
                trace=[{"step": 1, "tool": "read_memory"}],
            ))
        )
        runtime._safety_check = lambda *args, **kwargs: events.append("safety") or SafetyResult()  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Planner fallback blocked"):
                runtime.run(req, output_root=tmpdir)
            run_dirs = list(Path(tmpdir).iterdir())
            round_dir = run_dirs[0] / "round_1"
            failure = (round_dir / "planner_failure.json").read_text()
            plan = (round_dir / "plan.json").read_text()

        self.assertIn("stop_workload", events)
        self.assertNotIn("safety", events)
        self.assertIn("planner_fallback_blocked", failure)
        self.assertIn("fallback_blocked", plan)

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
        runtime._safety_check = lambda dag, snapshot, round_dir, request=None: events.append("safety") or SafetyResult()  # type: ignore[method-assign]
        runtime._execute = lambda dag, round_no, round_dir, request: events.append("execute") or {  # type: ignore[method-assign]
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

    def test_runtime_blocks_reflection_fallback_and_writes_failure_artifact(self):
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime.planner.reflect = lambda evaluation, request, memory_items: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(ReflectionFallbackError("Reflection fallback blocked: LLM failed"))
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            round_dir = Path(tmpdir)
            with self.assertRaisesRegex(RuntimeError, "Reflection fallback blocked"):
                runtime._reflect(EvaluationResult(success=False, reason="failed"), req, 1, round_dir)
            text = (round_dir / "reflection_failure.json").read_text()
        self.assertIn("reflection_fallback_blocked", text)
        self.assertIn("LLM failed", text)

    def _install_fake_taskgen_planner(self, runtime: DBMAGSRuntime, events: list[str]) -> None:
        runtime.planner.inspect = lambda request: (  # type: ignore[method-assign]
            events.append("inspect") or EnvironmentSnapshot(database=request.target_database, db_metrics={"max_connections": 100})
        )
        task = TaskSpec(
            task_id="traffic",
            task_type="traffic_surge",
            actions=[{"kind": "benchbase_burst", "duration_sec": 10}],
            metadata={"root_cause": "traffic_surge"},
        )
        dag = ExecutableTaskDAG(tasks={"traffic": task}, schedule={"traffic": 0.0})

        def fake_plan(request, snapshot, memory_items, reflection=None):
            events.append("plan")
            runtime.planner.last_plan_payload = {
                "target_path": request.target_path,
                "injected_nodes": request.injected_nodes,
                "task_specs": [to_jsonable(task)],
                "dependencies": [],
            }
            return dag, snapshot, []

        runtime.planner.plan = fake_plan  # type: ignore[method-assign]

    def test_taskgen_auto_reuses_existing_workload_when_qps_positive(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": True, "runner": "benchbase", "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: (_ for _ in ()).throw(AssertionError("runner should not be created"))  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "auto",
            "probe_interval_sec": 3.0,
            "qps": 12.0,
            "tps": 5.0,
            "existing_workload_detected": True,
            "started_new_workload": False,
        }
        self._install_fake_taskgen_planner(runtime, events)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runtime.generate_tasks_only(req, output_root=tmpdir)
            round_dir = Path(result["round_dir"])
            generated = json.loads((round_dir / "generated_tasks.json").read_text())
            trace = json.loads((round_dir / "workload_trace.json").read_text())
            detection = json.loads((round_dir / "taskgen_workload_detection.json").read_text())

        self.assertEqual(events, ["inspect", "plan"])
        self.assertEqual(trace["source"], "existing")
        self.assertTrue(detection["existing_workload_detected"])
        self.assertFalse(generated["workload_detection"]["started_new_workload"])

    def test_taskgen_auto_starts_workload_when_qps_zero(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={
                "enabled": True,
                "runner": "benchbase",
                "benchmark": "tpcc",
                "database": "tpcc",
                "warmup_sec": 60,
                "baseline_sec": 30,
                "injection_observe_sec": 60,
                "recovery_sec": 30,
                "sample_interval_sec": 5,
            },
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "auto",
            "probe_interval_sec": 3.0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
        }
        runtime._make_metrics_collector = lambda request, runner: (_ for _ in ()).throw(AssertionError("collector should not be used"))  # type: ignore[method-assign]
        runtime._sleep_phase = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("warmup should not run"))  # type: ignore[method-assign]
        runtime._safety_check = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("safety should not run"))  # type: ignore[method-assign]
        runtime._execute = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("execute should not run"))  # type: ignore[method-assign]
        runtime._evaluate = lambda **kwargs: (_ for _ in ()).throw(AssertionError("evaluate should not run"))  # type: ignore[method-assign]
        runtime._reflect = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reflection should not run"))  # type: ignore[method-assign]
        self._install_fake_taskgen_planner(runtime, events)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runtime.generate_tasks_only(req, output_root=tmpdir)
            round_dir = Path(result["round_dir"])
            generated = json.loads((round_dir / "generated_tasks.json").read_text())
            detection = json.loads((round_dir / "taskgen_workload_detection.json").read_text())

            self.assertTrue((round_dir / "request.json").exists())
            self.assertTrue((round_dir / "workload_config.json").exists())
            self.assertTrue((round_dir / "workload_trace.json").exists())
            self.assertTrue((round_dir / "static_snapshot.json").exists())
            self.assertTrue((round_dir / "snapshot.json").exists())
            self.assertTrue((round_dir / "react_trace.json").exists())
            self.assertTrue((round_dir / "plan.json").exists())
            self.assertTrue((round_dir / "task_dag.json").exists())
            self.assertEqual(generated["target_path"], req.target_path)
            self.assertEqual(generated["injected_nodes"], req.injected_nodes)
            self.assertEqual(generated["task_specs"][0]["task_id"], "traffic")
            self.assertIn("workload", generated)
            self.assertTrue(detection["started_new_workload"])

        self.assertEqual(events, ["start_workload", "inspect", "plan", "stop_workload"])

    def test_taskgen_fails_if_started_workload_exits_before_planning(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": True, "runner": "benchbase", "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: StopAfterStartRunner(events)  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "auto",
            "probe_interval_sec": 3.0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
        }
        runtime.planner.inspect = lambda request: (  # type: ignore[method-assign]
            events.append("inspect") or EnvironmentSnapshot(database=request.target_database)
        )
        runtime.planner.plan = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner should not run"))  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Background workload exited before Phase 10"):
                runtime.generate_tasks_only(req, output_root=tmpdir)
            run_dir = next(Path(tmpdir).iterdir())
            round_dir = run_dir / "round_1"
            trace_text = (round_dir / "workload_trace.json").read_text()

        self.assertEqual(events, ["start_workload", "stop_workload"])
        self.assertIn("taskgen_before_inspect", trace_text)
        self.assertIn("workload_exited_before_phase10", trace_text)

    def test_taskgen_requires_workload_enabled(self):
        runtime = DBMAGSRuntime(RuntimeConfig())
        req = ExperimentRequest(target_database="tpcc", workload={"enabled": False})
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "workload.enabled=true"):
                runtime.generate_tasks_only(req, output_root=tmpdir)

    def test_taskgen_reuse_fails_when_no_existing_workload(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": True, "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "reuse",
            "probe_interval_sec": 3.0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
        }
        runtime.planner.plan = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner should not run"))  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "requires an existing workload"):
                runtime.generate_tasks_only(req, output_root=tmpdir, workload_mode="reuse")
            run_dir = next(Path(tmpdir).iterdir())
            round_dir = run_dir / "round_1"
            detection = json.loads((round_dir / "taskgen_workload_detection.json").read_text())
            trace = json.loads((round_dir / "workload_trace.json").read_text())

        self.assertFalse(detection["existing_workload_detected"])
        self.assertEqual(trace["events"], [])
        self.assertEqual(events, [])

    def test_taskgen_start_always_starts_workload(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": True, "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "start",
            "probe_interval_sec": 0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
            "skipped_probe": True,
        }
        self._install_fake_taskgen_planner(runtime, events)

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime.generate_tasks_only(req, output_root=tmpdir, workload_mode="start")

        self.assertEqual(events, ["start_workload", "inspect", "plan", "stop_workload"])

    def test_taskgen_none_does_not_probe_or_start_workload(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": False, "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: (_ for _ in ()).throw(AssertionError("runner should not be created"))  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "none",
            "probe_interval_sec": 0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
            "skipped_probe": True,
        }
        self._install_fake_taskgen_planner(runtime, events)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runtime.generate_tasks_only(req, output_root=tmpdir, workload_mode="none")
            trace = json.loads((Path(result["round_dir"]) / "workload_trace.json").read_text())

        self.assertEqual(events, ["inspect", "plan"])
        self.assertEqual(trace["source"], "none")

    def test_taskgen_blocks_planner_fallback_and_stops_workload(self):
        events: list[str] = []
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "slow_query"],
            injected_nodes=["traffic_surge"],
            workload={"enabled": True, "benchmark": "tpcc", "database": "tpcc"},
        )
        runtime = DBMAGSRuntime(RuntimeConfig(planner_enabled=True, openai_api_key="key"))
        runtime._make_workload_runner = lambda request, round_dir: FakeRunner(events)  # type: ignore[method-assign]
        runtime._detect_taskgen_workload = lambda **kwargs: {  # type: ignore[method-assign]
            "mode": "auto",
            "probe_interval_sec": 3.0,
            "qps": 0.0,
            "tps": 0.0,
            "existing_workload_detected": False,
            "started_new_workload": False,
        }
        runtime.planner.inspect = lambda request: EnvironmentSnapshot(database=request.target_database)  # type: ignore[method-assign]
        runtime.planner.plan = lambda request, snapshot, memory_items, reflection=None: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(PlannerFallbackError(
                "Planner fallback blocked after tool loop failure: timeout",
                trace=[{"step": 1, "tool": "read_memory"}],
            ))
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Planner fallback blocked"):
                runtime.generate_tasks_only(req, output_root=tmpdir)
            run_dir = next(Path(tmpdir).iterdir())
            round_dir = run_dir / "round_1"
            self.assertTrue((round_dir / "planner_failure.json").exists())
            self.assertTrue((round_dir / "workload_trace.json").exists())

        self.assertIn("stop_workload", events)

    def test_cli_taskgen_invokes_runtime(self):
        from agent import cli

        payload = {
            "target_database": "tpcc",
            "workload": {"enabled": True, "benchmark": "tpcc", "database": "tpcc"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            request_path = Path(tmpdir) / "request.json"
            request_path.write_text(json.dumps(payload))
            captured = {}

            def fake_generate(self, request, output_root="experiment_runs/taskgen", workload_mode="auto"):
                captured["request"] = request
                captured["output_root"] = output_root
                captured["workload_mode"] = workload_mode
                return {"output_dir": str(Path(tmpdir) / "out"), "task_specs": [{"task_id": "t"}]}

            with patch.object(DBMAGSRuntime, "generate_tasks_only", fake_generate):
                code = cli.main([
                    "taskgen",
                    "--request",
                    str(request_path),
                    "--output-root",
                    str(Path(tmpdir) / "taskgen"),
                    "--workload-mode",
                    "reuse",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(captured["request"].target_database, "tpcc")
        self.assertTrue(str(captured["output_root"]).endswith("taskgen"))
        self.assertEqual(captured["workload_mode"], "reuse")

def _traffic_profile(**overrides):
    profile = {
        "benchmark": "tpcc",
        "database": "tpcc",
        "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
        "terminals": 8,
        "rate": 120.0,
        "duration_sec": 15,
        "transaction_mix": {
            "NewOrder": 50,
            "Payment": 45,
            "OrderStatus": 1,
            "Delivery": 2,
            "StockLevel": 2,
        },
        "mix_template": "workload_default",
        "rationale": "Write-heavy burst for lock and qps-drop propagation.",
    }
    profile.update(overrides)
    return profile


def _traffic_profile_for(benchmark, **overrides):
    if benchmark == "tpch":
        mix = {f"Q{i}": 1 for i in range(1, 23)}
        profile = {
            "benchmark": "tpch",
            "database": "tpch_1SF",
            "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpch_1SF_config.xml",
            "terminals": 2,
            "rate": "unlimited",
            "duration_sec": 15,
            "transaction_mix": mix,
            "mix_template": "workload_default",
            "rationale": "Same-benchmark TPCH burst for OLAP pressure.",
        }
    elif benchmark == "tatp":
        profile = {
            "benchmark": "tatp",
            "database": "tatp",
            "config_path": "tatp.xml",
            "terminals": 8,
            "rate": 10000,
            "duration_sec": 15,
            "transaction_mix": {
                "DeleteCallForwarding": 2,
                "GetAccessData": 35,
                "GetNewDestination": 10,
                "GetSubscriberData": 35,
                "InsertCallForwarding": 2,
                "UpdateLocation": 14,
                "UpdateSubscriberData": 2,
            },
            "mix_template": "workload_default",
            "rationale": "Same-benchmark TATP burst for OLTP pressure.",
        }
    else:
        profile = _traffic_profile()
    profile.update(overrides)
    return profile


def _benchbase_xml(benchmark, names, weights):
    tx = "".join(f"<transactiontype><name>{name}</name></transactiontype>" for name in names)
    return (
        "<?xml version=\"1.0\"?>"
        "<parameters>"
        f"<url>jdbc:mysql://127.0.0.1/old_{benchmark}?x=1</url>"
        "<terminals>1</terminals>"
        "<works><work><time>60</time><rate>100</rate>"
        f"<weights>{weights}</weights>"
        "</work></works>"
        f"<transactiontypes>{tx}</transactiontypes>"
        "</parameters>"
    )


if __name__ == "__main__":
    unittest.main()
