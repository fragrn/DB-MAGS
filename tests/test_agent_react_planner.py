from __future__ import annotations

import io
import http.client
import json
import unittest
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.planner import GlobalPlanner, PlannerFallbackError
from agent.reflection import SelfReflection
from agent.safety import estimate_dag_required_sec
from agent.tools import LLMTimeoutError, PLANNING_TOOL_NAMES, _extract_planner_json_payload, call_planning_tool, chat_tool_calling_loop, llm_generate, planning_tool_schemas
from agent.types import EnvironmentSnapshot, EvaluationResult, ExperimentRequest, ReflectionResult, SchemaInfo


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
        with self.assertRaisesRegex(ValueError, "not in target_path"):
            planner.plan(req, self.snapshot(), [])

    def test_request_validation_rejects_non_injectable_node(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="slow",
            target_database="tpcc",
            target_path=["missing_index", "poor_plan", "slow_query"],
            injected_nodes=["poor_plan"],
        )
        with self.assertRaisesRegex(ValueError, "not injectable"):
            planner.plan(req, self.snapshot(), [])

    def test_request_validation_rejects_disabled_excessive_index(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        req = ExperimentRequest(
            target_anomaly="slow_sql/excessive_index",
            target_database="tpcc",
            target_path=["excessive_index", "slow_query"],
            injected_nodes=["excessive_index"],
        )
        with self.assertRaisesRegex(ValueError, "not injectable"):
            planner.plan(req, self.snapshot(), [])

    def test_planning_tool_schemas_do_not_expose_config_or_execute(self):
        schemas = planning_tool_schemas()
        names = {item["function"]["name"] for item in schemas}
        self.assertNotIn("build_slow_sql_task", names)
        self.assertNotIn("get_benchbase_workload_defaults", names)
        self.assertNotIn("build_traffic_task", names)
        self.assertIn("probe_schema", names)
        self.assertIn("explain_sql", names)
        self.assertIn("build_task_dag", names)
        self.assertIn("check_safety", names)
        self.assertNotIn("execute_dag", names)
        self.assertNotIn("write_memory", names)
        self.assertNotIn("build_slow_sql_task", PLANNING_TOOL_NAMES)
        for schema in schemas:
            props = schema["function"]["parameters"]["properties"]
            self.assertNotIn("config", props)

    def test_native_tool_calling_generates_task_specs(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        planner = GlobalPlanner(config)
        req = self.request()
        task_spec = {
            "task_id": "slow_sql_missing_index",
            "task_type": "slow_sql",
            "actions": [
                {
                    "kind": "raw_sql_workload",
                    "sql": "SELECT * FROM orders WHERE o_comment LIKE '%abc%' LIMIT 100",
                    "concurrency": 8,
                    "duration_sec": 30,
                    "database": "tpcc",
                }
            ],
            "expected_metrics": {"slow_query_delta": ">=1"},
            "success_criteria": {"slow_query_delta": ">=1"},
            "risk_assessment": "low",
            "metadata": {
                "root_cause": "missing_index",
                "pattern": "weak_predicate",
                "table": "orders",
                "sql": "SELECT * FROM orders WHERE o_comment LIKE '%abc%' LIMIT 100",
            },
        }
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "target_path": req.target_path,
                                    "injected_nodes": req.injected_nodes,
                                    "task_specs": [task_spec],
                                    "dependencies": [],
                                    "reasoning": "Use weak predicate on an unindexed comment column.",
                                    "safety_check": {"approved": True, "reasons": [], "warnings": []},
                                }
                            ),
                        }
                    }
                ]
            },
        ]
        captured_payloads = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(req_obj, timeout=60):
            captured_payloads.append(json.loads(req_obj.data.decode("utf-8")))
            return FakeResponse(responses.pop(0))

        with patch("urllib.request.urlopen", fake_urlopen):
            dag, _snapshot, trace = planner.plan(req, self.snapshot(), [])

        self.assertEqual(list(dag.tasks), ["slow_sql_missing_index"])
        self.assertEqual(dag.tasks["slow_sql_missing_index"].actions[0]["duration_sec"], 30)
        self.assertEqual(planner.last_plan_payload["target_path"], req.target_path)
        self.assertEqual(planner.last_plan_payload["injected_nodes"], req.injected_nodes)
        self.assertTrue(any(step.action == "final_answer" for step in trace))
        self.assertFalse(any(message.get("role") == "tool" for message in captured_payloads[0]["messages"]))

    def test_initial_round_clamps_duration_to_observation_window(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key", planner_enabled=True))
        req = ExperimentRequest(
            target_anomaly="missing_index_chain",
            target_database="tpcc_10W",
            target_path=["missing_index", "poor_plan", "slow_query", "qps_drop"],
            injected_nodes=["missing_index"],
            max_duration_sec=30,
            workload={
                "enabled": True,
                "benchmark": "tpcc",
                "database": "tpcc_10W",
                "injection_observe_sec": 15,
            },
        )
        task_spec = {
            "task_id": "missing_index_initial",
            "task_type": "slow_sql",
            "actions": [{
                "kind": "raw_sql_workload",
                "database": "tpcc_10W",
                "sql": "SELECT COUNT(*) FROM orders WHERE o_comment LIKE '%x%'",
                "concurrency": 8,
                "duration_sec": 30,
            }],
            "expected_metrics": {},
            "success_criteria": {},
            "risk_assessment": "low",
            "metadata": {"root_cause": "missing_index"},
        }

        with patch("agent.tools.chat_tool_calling_loop", return_value={
            "json_payload": {"task_specs": [task_spec], "dependencies": []},
            "trace": [],
        }):
            dag, _snapshot, trace = planner.plan(req, self.snapshot(), [])

        task = dag.tasks["missing_index_initial"]
        self.assertEqual(task.actions[0]["duration_sec"], 15)
        self.assertEqual(task.actions[0]["concurrency"], 8)
        self.assertEqual(task.metadata["planner_duration_limit_sec"], 15)
        self.assertEqual(task.metadata["planner_duration_clamps"][0]["planning_round"], "initial")
        self.assertTrue(any(step.action == "clamp_planner_duration" for step in trace))
        required_sec, timing_reasons = estimate_dag_required_sec({
            "tasks": {task_id: value.to_dict() for task_id, value in dag.tasks.items()},
            "schedule": dag.schedule,
        })
        self.assertEqual(required_sec, 15.0)
        self.assertEqual(timing_reasons, [])

    def test_retry_clamps_llm_duration_to_observation_window(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        planner = GlobalPlanner(config)
        req = ExperimentRequest(
            target_anomaly="traffic_surge_chain",
            target_database="tpcc_10W",
            target_path=["traffic_surge", "threads_concurrency_up", "lock_contention", "slow_query", "qps_drop"],
            injected_nodes=["traffic_surge"],
            max_duration_sec=30,
            workload={
                "enabled": True,
                "benchmark": "tpcc",
                "database": "tpcc_10W",
                "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
                "injection_observe_sec": 15,
            },
        )
        task_spec = {
            "task_id": "traffic_retry",
            "task_type": "traffic_surge",
            "actions": [{
                "kind": "benchbase_burst_command",
                "benchmark": "tpcc",
                "database": "tpcc_10W",
                "command": ["java", "-jar", "wrong.jar", "-b", "tpcc", "-c", "wrong.xml"],
                "duration_sec": 30,
                "terminals": 64,
                "rate": 400,
            }],
            "expected_metrics": {},
            "success_criteria": {},
            "risk_assessment": "medium",
            "metadata": {"root_cause": "traffic_surge"},
        }
        reflection = ReflectionResult(failure_reason="traffic was too weak")

        with patch("agent.tools.chat_tool_calling_loop", return_value={
            "json_payload": {
                "task_specs": [task_spec],
                "dependencies": [],
                "reasoning": "Increase active traffic pressure.",
            },
            "trace": [],
        }):
            dag, _snapshot, trace = planner.plan(req, self.snapshot(), [], reflection=reflection)

        action = dag.tasks["traffic_retry"].actions[0]
        metadata = dag.tasks["traffic_retry"].metadata
        self.assertEqual(action["duration_sec"], 15)
        self.assertEqual(action["terminals"], 64)
        self.assertEqual(action["rate"], 400)
        self.assertEqual(metadata["planner_duration_limit_sec"], 15)
        self.assertEqual(metadata["planner_duration_clamps"][0]["planning_round"], "retry")
        self.assertEqual(metadata["planner_duration_clamps"][0]["original_duration_sec"], 30)
        self.assertTrue(any(step.action == "clamp_planner_duration" for step in trace))
        required_sec, timing_reasons = estimate_dag_required_sec({
            "tasks": {task_id: task.to_dict() for task_id, task in dag.tasks.items()},
            "schedule": dag.schedule,
        })
        self.assertEqual(required_sec, 15.0)
        self.assertEqual(timing_reasons, [])

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
        retry_context = planner._build_llm_context(
            req,
            self.snapshot(),
            [],
            ReflectionResult(failure_reason="retry"),
        )
        initial_context = planner._build_llm_context(req, self.snapshot(), [], None)

        self.assertIn("planning_round: retry", retry_context)
        self.assertIn("duration_limit_sec: 15.0", retry_context)
        self.assertIn("instead of duration", retry_context)
        self.assertIn("planning_round: initial", initial_context)
        self.assertIn("duration_limit_sec: 15.0", initial_context)

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

    def test_old_builder_tool_call_is_blocked(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            call_planning_tool(
                "build_slow_sql_task",
                config=config,
                arguments={"database": "tpcc", "table": "orders"},
            )

    def test_planner_disabled_blocks_fallback(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        with self.assertRaisesRegex(PlannerFallbackError, "planner_enabled=false"):
            planner.plan(self.request(), self.snapshot(), [])

    def test_tool_loop_error_blocks_fallback(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        planner = GlobalPlanner(config)
        with patch("agent.tools.chat_tool_calling_loop", return_value={
            "error": "The read operation timed out",
            "trace": [{"step": 1, "tool": "read_memory", "result": []}],
        }):
            with self.assertRaisesRegex(PlannerFallbackError, "The read operation timed out"):
                planner.plan(self.request(), self.snapshot(), [])
        self.assertTrue(any(step.action == "fallback_blocked" for step in planner._react_trace))

    def test_invalid_action_shape_blocks_planner_without_attribute_error(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        planner = GlobalPlanner(config)
        bad_spec = {
            "task_id": "bad_missing_index",
            "task_type": "raw_sql_workload",
            "actions": ["SELECT 1"],
            "expected_metrics": {},
            "success_criteria": {},
            "risk_assessment": "medium",
            "metadata": {"root_cause": "missing_index"},
        }
        with patch("agent.tools.chat_tool_calling_loop", return_value={
            "json_payload": {"task_specs": [bad_spec], "dependencies": []},
            "trace": [{"step": 1, "tool": "final_answer", "content": "{}"}],
        }):
            with self.assertRaisesRegex(PlannerFallbackError, "non-object action"):
                planner.plan(self.request(), self.snapshot(), [])
        self.assertTrue(any(step.action == "fallback_blocked" for step in planner._react_trace))

    def test_chat_tool_calling_timeout_raises(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)

        def timeout_urlopen(req_obj, timeout=60):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", timeout_urlopen):
            with self.assertRaisesRegex(LLMTimeoutError, "timed out after 120s"):
                chat_tool_calling_loop(config, "system", "user", max_steps=1)

    def test_chat_tool_calling_retries_timeout_once(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        responses = [
            TimeoutError("transient timeout"),
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"ok": True}),
                        }
                    }
                ]
            },
        ]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def flaky_urlopen(req_obj, timeout=60):
            item = responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FakeResponse(item)

        with patch("urllib.request.urlopen", flaky_urlopen):
            result = chat_tool_calling_loop(config, "system", "user", max_steps=1)

        self.assertEqual(result["json_payload"], {"ok": True})

    def test_chat_tool_calling_retries_remote_disconnect(self):
        config = RuntimeConfig(openai_api_key="key", planner_enabled=True)
        responses = [
            http.client.RemoteDisconnected("remote closed"),
            {
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps({"ok": True})}}
                ]
            },
        ]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def flaky_urlopen(req_obj, timeout=60):
            item = responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FakeResponse(item)

        with patch("urllib.request.urlopen", flaky_urlopen):
            result = chat_tool_calling_loop(config, "system", "user", max_steps=1)

        self.assertEqual(result["json_payload"], {"ok": True})

    def test_planner_final_json_extractor_accepts_prose_wrapped_json(self):
        payload = {"task_specs": [{"task_id": "t1"}], "dependencies": []}
        text = "Here is the plan:\n" + json.dumps(payload) + "\nDone."
        self.assertEqual(_extract_planner_json_payload(text), payload)

    def test_planner_final_json_extractor_prefers_object_with_task_specs(self):
        first = {"note": "not the final plan"}
        second = {"task_specs": [{"task_id": "t1"}], "dependencies": []}
        text = json.dumps(first) + "\n" + json.dumps(second)
        self.assertEqual(_extract_planner_json_payload(text), second)

    def test_planner_final_json_extractor_rejects_invalid_text(self):
        with self.assertRaises(json.JSONDecodeError):
            _extract_planner_json_payload("no json here")

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

        context = planner._build_llm_context(self.request(), snapshot, [], reflection)

        self.assertIn("needs stronger surge", context)
        self.assertIn('"terminals": 16', context)
        self.assertIn('"qps": 100', context)
        self.assertNotIn("RAW_TEXT_SHOULD_NOT_BE_INCLUDED", context)
        self.assertNotIn('"samples"', context)
        self.assertNotIn("Unused_large_value", context)

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
            "risk_assessment": "low",
            "metadata": {"root_cause": "backup"},
        }
        filtered = planner._filter_task_specs([spec], req)
        self.assertEqual(filtered[0]["task_id"], "backup_echo")


if __name__ == "__main__":
    unittest.main()
