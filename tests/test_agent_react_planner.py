from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.planner import GlobalPlanner
from agent.tools import PLANNING_TOOL_NAMES, planning_tool_schemas
from agent.types import EnvironmentSnapshot, ExperimentRequest, SchemaInfo


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

    def test_non_requested_task_spec_is_dropped_and_missing_requested_fails(self):
        planner = GlobalPlanner(RuntimeConfig(planner_enabled=False))
        bad_spec = {
            "task_id": "lock_hot_update",
            "task_type": "lock_conflict",
            "metadata": {"root_cause": "hot_update"},
        }
        with self.assertRaisesRegex(ValueError, "missing TaskSpecs"):
            planner._filter_task_specs([bad_spec], self.request())


if __name__ == "__main__":
    unittest.main()
