from __future__ import annotations

import io
import http.client
import json
import unittest
from unittest.mock import patch

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.planner import GlobalPlanner, PlannerFallbackError
from AnomalyGraphAgent.reflection import SelfReflection
from AnomalyGraphAgent.safety import estimate_dag_required_sec
from AnomalyGraphAgent.tools import LLMTimeoutError, llm_generate
from AnomalyGraphAgent.types import EnvironmentSnapshot, EvaluationResult, ExperimentRequest, ReflectionResult, SchemaInfo


class AgentReactPlannerTests(unittest.TestCase):
    def snapshot(self) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            database="tpcc",
            schema=SchemaInfo(
                database="tpcc",
                tables={
                    "orders": {
                        "columns": [
                            {"name": "o_id", "type": "int", "key": "PRI"},
                            {"name": "o_comment", "type": "varchar", "key": ""},
                        ],
                        "indexes": [{"index_name": "PRIMARY", "column": "o_id"}],
                    }
                },
            ),
            db_metrics={"max_connections": 200, "Threads_connected": 5, "Threads_running": 1},
            workload_status={"qps": 1.0},
            os_metrics={"cpu_usage": {"usage_ratio": 0.1}},
            db_version="8.0",
            max_connections=200,
        )

    def request(self) -> ExperimentRequest:
        return ExperimentRequest(
            target_anomaly="missing_index",
            target_database="tpcc",
            target_path=["missing_index", "poor_plan", "slow_query"],
            injected_nodes=["missing_index"],
            max_retry_rounds=1,
        )

    def test_request_validation_rejects_injected_node_outside_path(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "threads_concurrency_up", "lock_contention", "slow_query"],
            injected_nodes=["traffic_surge", "long_tx"],
        )
        with self.assertRaisesRegex(PlannerFallbackError, "not in target_path"):
            planner.plan(req, self.snapshot(), [])

    def test_request_validation_rejects_non_injectable_node(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="slow",
            target_database="tpcc",
            target_path=["missing_index", "poor_plan", "slow_query"],
            injected_nodes=["poor_plan"],
        )
        with self.assertRaisesRegex(PlannerFallbackError, "not injectable"):
            planner.plan(req, self.snapshot(), [])

    def test_request_validation_rejects_disabled_excessive_index(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="slow_sql/excessive_index",
            target_database="tpcc",
            target_path=["excessive_index", "slow_query"],
            injected_nodes=["excessive_index"],
        )
        with self.assertRaisesRegex(PlannerFallbackError, "not injectable"):
            planner.plan(req, self.snapshot(), [])



    def _query_intent(self, duration=30):
        return {"intents": [{
            "node": "missing_index", "strategy": "unindexed_filter",
            "target": {"tables": ["orders"], "columns": ["o_comment"]},
            "load": {"concurrency": 8, "duration_sec": duration},
            "sql": {"query": "SELECT COUNT(*) FROM orders WHERE o_comment LIKE '%x%'"},
            "expected_evidence": ["poor_plan", "slow_query"],
        }]}

    def test_initial_round_clamps_duration_to_observation_window(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key"))
        req = self.request()
        req.max_duration_sec = 30
        req.workload = {"enabled": True, "benchmark": "tpcc", "database": "tpcc", "injection_observe_sec": 15}
        with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": self._query_intent()}), \
             patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": []}):
            dag, _, _ = planner.plan(req, self.snapshot())
        task = next(iter(dag.tasks.values()))
        self.assertEqual(task.actions[0]["duration_sec"], 15)
        self.assertEqual(task.actions[0]["concurrency"], 8)
        changes = planner.last_plan_payload["compilation"]["normalizations"]
        self.assertEqual(changes[0]["original"], 30)
        self.assertEqual(changes[0]["final"], 15)
        required, errors = estimate_dag_required_sec({"tasks": {task.task_id: task.to_dict()}, "schedule": dag.schedule})
        self.assertEqual(required, 15)
        self.assertEqual(errors, [])

    def test_retry_clamps_llm_duration_to_observation_window(self):
        from AnomalyGraphAgent.intent_compiler import CompilationContext
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key"))
        req = ExperimentRequest(target_database="tpcc", target_path=["traffic_surge", "threads_concurrency_up"],
                                injected_nodes=["traffic_surge"], max_duration_sec=30,
                                workload={"enabled": True, "benchmark": "tpcc", "database": "tpcc", "injection_observe_sec": 15})
        profile = {"benchmark": "tpcc", "database": "tpcc", "config_path": "/tmp/tpcc.xml",
                   "terminals": 4, "rate": 100, "duration_sec": 30,
                   "transaction_mix": {"NewOrder": 45, "Payment": 55},
                   "mix_template": "baseline", "rationale": "baseline mix"}
        ctx = CompilationContext(req, planner.config, "retry-run", round_no=2,
                                 benchbase_profile=profile, transaction_types=("NewOrder", "Payment"))
        payload = {"intents": [{"node": "traffic_surge", "strategy": "increase_client_concurrency",
                               "target": None, "sql": None, "expected_evidence": [],
                               "load": {"duration_sec": 30, "concurrency": 64, "rate": 400}}]}
        with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}):
            dag, _, _ = planner.plan(req, self.snapshot(), reflection=ReflectionResult(failure_reason="weak traffic"),
                                     compilation_context=ctx)
        action = next(iter(dag.tasks.values())).actions[0]
        self.assertEqual(action["duration_sec"], 15)
        self.assertEqual(action["profile"]["duration_sec"], 15)
        self.assertEqual(action["profile"]["terminals"], 64)
        self.assertEqual(action["profile"]["rate"], 400)

    def test_invalid_json_is_repaired_once_before_local_validation(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key"))
        responses = [{"text": '{"intents": ['}, {"json_payload": self._query_intent(5)}]
        with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=responses) as mocked, \
             patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": []}):
            dag, _, _ = planner.plan(self.request(), self.snapshot())
        self.assertEqual(len(dag.tasks), 1)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(planner.planner_json_repair_attempts, 1)
        self.assertIn("Intent", mocked.call_args.kwargs["system_prompt"])

    def test_planner_duration_clamp_covers_raw_and_nested_actions(self):
        specs = [{
            "task_id": "mixed",
            "metadata": {"root_cause": "traffic_surge"},
            "actions": [
                {"kind": "raw_command", "duration_sec": 30},
                {"kind": "raw_sql_workload", "duration_sec": 20},
                {"kind": "raw_transaction_script", "duration_sec": 16},
                {"kind": "benchbase_burst_command", "duration_sec": 45},
                {
                    "kind": "benchbase_burst",
                    "duration_sec": 25,
                    "profile": {"duration_sec": 35},
                },
                {"kind": "raw_sql_workload", "duration_sec": 0},
                {"kind": "raw_sql_workload", "duration_sec": "invalid"},
                {"kind": "raw_sql_workload"},
            ],
        }]

        clamps = GlobalPlanner._clamp_planner_task_durations(specs, 15.0, "retry")
        actions = specs[0]["actions"]

        self.assertEqual([actions[i]["duration_sec"] for i in range(5)], [15, 15, 15, 15, 15])
        self.assertEqual(actions[4]["profile"]["duration_sec"], 15)
        self.assertEqual(actions[5]["duration_sec"], 0)
        self.assertEqual(actions[6]["duration_sec"], "invalid")
        self.assertNotIn("duration_sec", actions[7])
        self.assertEqual(len(clamps), 6)
        self.assertEqual(specs[0]["metadata"]["planner_duration_limit_sec"], 15)

    def test_planner_duration_limit_uses_max_duration_without_workload(self):
        request = ExperimentRequest(max_duration_sec=10, workload={})
        specs = [{
            "task_id": "raw",
            "metadata": {"root_cause": "missing_index"},
            "actions": [
                {"kind": "raw_sql_workload", "duration_sec": 20},
                {"kind": "raw_sql_workload", "duration_sec": 8},
            ],
        }]

        limit = GlobalPlanner._planner_duration_limit(request)
        clamps = GlobalPlanner._clamp_planner_task_durations(specs, limit, "initial")

        self.assertEqual(limit, 10)
        self.assertEqual(specs[0]["actions"][0]["duration_sec"], 10)
        self.assertEqual(specs[0]["actions"][1]["duration_sec"], 8)
        self.assertEqual(len(clamps), 1)

    def test_planner_context_exposes_duration_hard_limit_for_all_rounds(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "threads_concurrency_up"],
            injected_nodes=["traffic_surge"],
            max_duration_sec=30,
            workload={"enabled": True, "injection_observe_sec": 15},
        )
        retry_context = planner._build_planner_context(
            req, self.snapshot(), [], ReflectionResult(failure_reason="retry")
        )
        initial_context = planner._build_planner_context(req, self.snapshot(), [], None)

        self.assertEqual(retry_context["duration_limit_sec"], 15)
        self.assertEqual(initial_context["duration_limit_sec"], 15)
        self.assertEqual(retry_context["request"]["target_path"], req.target_path)
        self.assertIn("schema", retry_context["environment"])

    def test_reflection_prompt_forbids_extending_observation_window(self):
        reflector = SelfReflection(RuntimeConfig())
        request = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "threads_concurrency_up"],
            injected_nodes=["traffic_surge"],
            max_duration_sec=30,
            workload={"enabled": True, "injection_observe_sec": 15},
        )

        prompt = reflector._build_reflection_prompt(
            EvaluationResult(success=False, reason="weak traffic"),
            request,
            [],
        )

        self.assertIn("<= 15 seconds", prompt)
        self.assertIn("Do not suggest extending the observation window", prompt)


    def test_planner_disabled_blocks_fallback(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        with self.assertRaisesRegex(PlannerFallbackError, "planner_enabled=false"):
            planner.plan(self.request(), self.snapshot(), [])


    def test_invalid_action_shape_blocks_planner_without_attribute_error(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        planner = GlobalPlanner(config)
        bad_spec = {
            "task_id": "bad_missing_index",
            "task_type": "raw_sql_workload",
            "actions": ["SELECT 1"],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "missing_index"},
        }
        with patch("AnomalyGraphAgent.tools.llm_generate", return_value={
            "json_payload": {"task_specs": [bad_spec], "dependencies": []},
            "trace": [{"step": 1, "tool": "final_answer", "content": "{}"}],
        }):
            with self.assertRaisesRegex(PlannerFallbackError, "only intents"):
                planner.plan(self.request(), self.snapshot(), [])
        self.assertTrue(any(step.action == "planner_validation_failed" for step in planner._planner_trace))







    def test_llm_generate_timeout_raises(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)

        def timeout_urlopen(req_obj, timeout=60):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", timeout_urlopen):
            with self.assertRaisesRegex(LLMTimeoutError, "timed out after 120s"):
                llm_generate(config, "system", "user")

    def test_llm_context_compacts_reflection_and_workload_status(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        snapshot = self.snapshot()
        snapshot.workload_status = {
            "phase": "baseline",
            "sample_count": 2,
            "samples": [{"large": "x" * 5000}],
            "summary": {
                "qps": 100,
                "p95_latency_ms": 20,
                "db_metrics": {
                    "Threads_connected": 10,
                    "Threads_running": 2,
                    "Slow_queries": 0,
                    "Unused_large_value": "y" * 5000,
                },
            },
        }
        reflection = ReflectionResult(
            failure_reason="needs stronger surge",
            suggested_changes=["increase terminals"],
            task_parameter_updates={"traffic_surge": {"terminals": 16}},
            risk_warning="watch connections",
            raw_text="RAW_TEXT_SHOULD_NOT_BE_INCLUDED" + "z" * 5000,
        )

        context = planner._build_planner_context(self.request(), snapshot, [], reflection)

        self.assertEqual(context["reflection"]["failure_reason"], "needs stronger surge")
        self.assertEqual(context["workload"]["baseline"]["summary"]["qps"], 100)
        self.assertIn("schema", context["environment"])
        self.assertIn("duration_limit_sec", context)

    def test_non_requested_task_spec_is_dropped_and_missing_requested_fails(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        bad_spec = {
            "task_id": "lock_hot_update",
            "task_type": "lock_conflict",
            "metadata": {"root_cause": "hot_update"},
        }
        with self.assertRaisesRegex(ValueError, "missing TaskSpecs"):
            planner._filter_task_specs([bad_spec], self.request())

    def test_traffic_surge_rejects_legacy_workload_ramp(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpcc",
            target_path=["traffic_surge", "threads_concurrency_up"],
            injected_nodes=["traffic_surge"],
        )
        legacy_spec = {
            "task_id": "traffic",
            "task_type": "traffic_surge",
            "actions": [{"kind": "workload_ramp", "sql": "SELECT 1", "duration_sec": 10}],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "traffic_surge"},
        }
        with self.assertRaisesRegex(ValueError, "unsupported action kind"):
            planner._filter_task_specs([legacy_spec], req)

    def test_traffic_surge_rejects_benchmark_mismatch_with_background_workload(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="traffic",
            target_database="tpch_1SF",
            target_path=["traffic_surge", "threads_concurrency_up"],
            injected_nodes=["traffic_surge"],
            workload={
                "enabled": True,
                "benchmark": "tpch",
                "database": "tpch_1SF",
                "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpch_1SF_config.xml",
            },
        )
        tpcc_spec = {
            "task_id": "traffic",
            "task_type": "traffic_surge",
            "actions": [
                {
                    "kind": "benchbase_burst_command",
                    "benchmark": "tpcc",
                    "database": "tpcc_10W",
                    "command": ["java", "-jar", "benchbase.jar", "-b", "tpcc"],
                    "duration_sec": 10,
                    "terminals": 4,
                    "rate": 100,
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "traffic_surge"},
        }
        with self.assertRaisesRegex(ValueError, "benchmark must match background workload"):
            planner._filter_task_specs([tpcc_spec], req)

    def test_resource_rejects_non_chaosblade_raw_command(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False, chaosblade_path="/opt/blade"))
        req = ExperimentRequest(
            target_anomaly="resource",
            target_database="tpcc",
            target_path=["resource_cpu", "resource_bottleneck_cpu"],
            injected_nodes=["resource_cpu"],
        )
        spec = {
            "task_id": "resource_cpu_python",
            "task_type": "resource_cpu",
            "actions": [
                {
                    "kind": "raw_command",
                    "command": ["python3", "-c", "while True: pass"],
                    "duration_sec": 10,
                    "cleanup_command": [],
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "resource_cpu"},
        }
        with self.assertRaisesRegex(ValueError, "RuntimeConfig\\.chaosblade_path"):
            planner._filter_task_specs([spec], req)

    def test_resource_accepts_llm_generated_chaosblade_raw_command(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False, chaosblade_path="/opt/blade"))
        req = ExperimentRequest(
            target_anomaly="resource",
            target_database="tpcc",
            target_path=["resource_cpu", "resource_bottleneck_cpu"],
            injected_nodes=["resource_cpu"],
        )
        spec = {
            "task_id": "resource_cpu_blade",
            "task_type": "resource_cpu",
            "actions": [
                {
                    "kind": "raw_command",
                    "command": ["/opt/blade", "create", "cpu", "fullload", "--cpu-percent", "80", "--timeout", "10", "--uid", "cpu_uid_1"],
                    "duration_sec": 10,
                    "cleanup_command": ["/opt/blade", "destroy", "cpu_uid_1"],
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "resource_cpu"},
        }
        filtered = planner._filter_task_specs([spec], req)
        self.assertEqual(filtered[0]["task_id"], "resource_cpu_blade")

    def test_resource_rejects_cleanup_uid_mismatch(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False, chaosblade_path="/opt/blade"))
        req = ExperimentRequest(
            target_anomaly="resource",
            target_database="tpcc",
            target_path=["resource_io", "resource_bottleneck_io"],
            injected_nodes=["resource_io"],
        )
        spec = {
            "task_id": "resource_io_blade",
            "task_type": "resource_io",
            "actions": [
                {
                    "kind": "raw_command",
                    "command": ["/opt/blade", "create", "disk", "burn", "--read", "--write", "--path", "/tmp", "--size", "100M", "--timeout", "10", "--uid=io_uid_1"],
                    "duration_sec": 10,
                    "cleanup_command": ["/opt/blade", "destroy", "other_uid"],
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "resource_io"},
        }
        with self.assertRaisesRegex(ValueError, "same ChaosBlade uid"):
            planner._filter_task_specs([spec], req)

    def test_resource_rejects_bare_blade_command(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False, chaosblade_path="/opt/blade"))
        req = ExperimentRequest(
            target_anomaly="resource",
            target_database="tpcc",
            target_path=["resource_io", "resource_bottleneck_io"],
            injected_nodes=["resource_io"],
        )
        spec = {
            "task_id": "resource_io_blade",
            "task_type": "resource_io",
            "actions": [
                {
                    "kind": "raw_command",
                    "command": ["blade", "create", "disk", "fill", "--path", "/tmp", "--size", "100M", "--uid=io_uid_1"],
                    "duration_sec": 10,
                    "cleanup_command": ["blade", "destroy", "io_uid_1"],
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "resource_io"},
        }
        with self.assertRaisesRegex(ValueError, "RuntimeConfig\\.chaosblade_path"):
            planner._filter_task_specs([spec], req)

    def test_resource_rejects_non_raw_command_action_kind(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="resource",
            target_database="tpcc",
            target_path=["resource_memory", "slow_query"],
            injected_nodes=["resource_memory"],
        )
        spec = {
            "task_id": "resource_memory_sql",
            "task_type": "resource_memory",
            "actions": [{"kind": "raw_sql_workload", "database": "tpcc", "sql": "SELECT 1", "duration_sec": 10}],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "medium",
            "metadata": {"root_cause": "resource_memory"},
        }
        with self.assertRaisesRegex(ValueError, "must use raw_command"):
            planner._filter_task_specs([spec], req)

    def test_non_resource_raw_command_does_not_require_chaosblade(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="backup",
            target_database="tpcc",
            target_path=["backup", "maintenance_conflict"],
            injected_nodes=["backup"],
        )
        spec = {
            "task_id": "backup_echo",
            "task_type": "backup",
            "actions": [{"kind": "raw_command", "command": ["echo", "ok"], "duration_sec": 1}],
            "expected_metrics": {},
            "success_criteria": {},
            "start_after_sec": 0,
            "dependencies": [],
            "cleanup_actions": [],
            "risk_assessment": "low",
            "metadata": {"root_cause": "backup"},
        }
        filtered = planner._filter_task_specs([spec], req)
        self.assertEqual(filtered[0]["task_id"], "backup_echo")


if __name__ == "__main__":
    unittest.main()
