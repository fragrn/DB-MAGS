from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.planner import GlobalPlanner, PlannerFallbackError
from agent.tools import LLMTimeoutError, PLANNING_TOOL_NAMES, build_traffic_task, chat_tool_calling_loop, llm_generate, planning_tool_schemas
from agent.types import EnvironmentSnapshot, ExperimentRequest, ReflectionResult, SchemaInfo


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

    def test_planning_tool_schemas_do_not_expose_config_or_execute(self):
        schemas = planning_tool_schemas()
        names = {item["function"]["name"] for item in schemas}
        self.assertIn("build_slow_sql_task", names)
        self.assertIn("get_benchbase_workload_defaults", names)
        self.assertIn("build_traffic_task", names)
        self.assertNotIn("execute_dag", names)
        self.assertNotIn("write_memory", names)
        self.assertIn("build_slow_sql_task", PLANNING_TOOL_NAMES)
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
                    "kind": "sql_workload",
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
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "build_slow_sql_task",
                                        "arguments": json.dumps(
                                            {
                                                "database": "tpcc",
                                                "task_id": "slow_sql_missing_index",
                                                "root_cause": "missing_index",
                                                "table": "orders",
                                                "predicate": "o_comment LIKE '%abc%'",
                                                "pattern": "weak_predicate",
                                                "limit": 100,
                                                "concurrency": 8,
                                                "duration_sec": 30,
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
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
        self.assertEqual(planner.last_plan_payload["target_path"], req.target_path)
        self.assertEqual(planner.last_plan_payload["injected_nodes"], req.injected_nodes)
        self.assertTrue(any(step.action == "build_slow_sql_task" for step in trace))
        self.assertTrue(any(message.get("role") == "tool" for message in captured_payloads[1]["messages"]))

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
            "metadata": {"root_cause": "traffic_surge"},
        }
        with self.assertRaisesRegex(ValueError, "benchbase_burst"):
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
        tpcc_spec = build_traffic_task(
            RuntimeConfig(),
            profile={
                "benchmark": "tpcc",
                "database": "tpcc_10W",
                "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
                "terminals": 4,
                "rate": 100,
                "duration_sec": 10,
                "transaction_mix": {
                    "NewOrder": 45,
                    "Payment": 43,
                    "OrderStatus": 4,
                    "Delivery": 4,
                    "StockLevel": 4,
                },
                "mix_template": "workload_default",
                "rationale": "wrong benchmark",
            },
            task_id="traffic",
        )
        with self.assertRaisesRegex(ValueError, "benchmark must match background workload"):
            planner._filter_task_specs([tpcc_spec], req)


if __name__ == "__main__":
    unittest.main()
