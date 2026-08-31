from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from dbmags.agents import build_default_agents
from dbmags.config import RuntimeConfig
from dbmags.dag import build_task_dag, topological_order
from dbmags.evaluator import Evaluator
from dbmags.executor.chaosblade import ChaosBlade
from dbmags.executor import Executor
from dbmags.llm import ChatCompletionsClient
from dbmags.planner import GlobalPlanner
from dbmags.runtime import DBMAGSRuntime
from dbmags.safety import SafetyChecker
from dbmags.memory import MemoryStore
from dbmags.reflection import SelfReflection
from dbmags.types import EnvironmentSnapshot, EvaluationResult, ExecutableTaskDAG, ExecutionTrace, ExperimentRequest, GlobalPlan, SafetyResult, TaskDAGEdge, TaskSpec


class ChatCompletionsTests(unittest.TestCase):
    def test_chat_completions_payload_uses_chat_endpoint(self):
        config = RuntimeConfig(openai_api_key="key", openai_model="gpt-5", planner_enabled=True)
        client = ChatCompletionsClient(config)
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", {"hello": "world"}, 0.2)
        self.assertFalse(result.used_fallback)
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertIn("messages", captured["payload"])
        self.assertNotIn("input", captured["payload"])
        self.assertNotIn("temperature", captured["payload"])
        self.assertEqual(result.json_payload, {"ok": True})

    def test_chat_client_falls_back_on_http_error(self):
        config = RuntimeConfig(openai_api_key="key", openai_model="custom", planner_enabled=True)
        client = ChatCompletionsClient(config)

        def fake_urlopen(req, timeout=60):
            body = json.dumps({"error": {"message": "boom"}}).encode("utf-8")
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", hdrs=None, fp=io.BytesIO(body))

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", {"hello": "world"}, 0.2)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.error_type, "http_error")


class PlannerAgentDAGTests(unittest.TestCase):
    def snapshot(self):
        return EnvironmentSnapshot(
            database="dbmags_tpcc_base",
            version="8.0",
            schema={
                "tables": {
                    "district": {"columns": [{"name": "d_id", "data_type": "int", "column_key": "PRI"}]},
                    "orders": {"columns": [{"name": "o_id", "data_type": "int", "column_key": "PRI"}, {"name": "o_comment", "data_type": "varchar", "column_key": ""}]},
                }
            },
            table_stats={"tables": [{"table_name": "orders", "table_rows": 1000}, {"table_name": "district", "table_rows": 10}]},
            db_metrics={"Threads_connected": 10, "variables": {"max_connections": 200}},
            os_metrics={"cpu_usage": {"percent": 10}},
        )

    def test_fallback_planner_builds_causal_chain_plan(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        request = ExperimentRequest(
            target_mode="causal_chain",
            target_chain=["traffic_surge", "connections_up", "lock_contention", "slow_query", "qps_drop"],
            user_constraints={"max_duration_sec": 300},
        )
        plan = planner.plan(request, self.snapshot())
        self.assertEqual(plan.root_causes_to_inject, ["traffic_surge", "lock_contention", "slow_query"])
        self.assertIn("TrafficSurgeAgent", plan.task_agents)
        self.assertIn(["traffic_surge_task", "lock_conflict_task"], plan.task_dependencies)

    def test_llm_plan_rejects_invalid_schema(self):
        class FakeLLM:
            def available(self):
                return True

            def generate_json(self, *_args, **_kwargs):
                return type("Result", (), {"json_payload": {"mode": "causal_chain", "task_agents": ["BadAgent"]}, "used_fallback": False})()

        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key"), llm_client=FakeLLM())
        request = ExperimentRequest(target_chain=["traffic_surge"])
        plan = planner.plan(request, self.snapshot())
        self.assertFalse(plan.llm_used)
        self.assertEqual(plan.root_causes_to_inject, ["traffic_surge"])

    def test_dba_description_request_uses_llm_to_generate_plan(self):
        captured = {}

        class FakeLLM:
            def available(self):
                return True

            def generate_json(self, system, payload, *_args):
                captured["system"] = system
                captured["payload"] = payload
                return type(
                    "Result",
                    (),
                    {
                        "json_payload": {
                            "mode": "causal_chain",
                            "root_causes_to_inject": ["traffic_surge", "lock_contention"],
                            "effects_to_observe": ["connections_up", "slow_query", "qps_drop"],
                            "task_agents": ["TrafficSurgeAgent", "LockConflictAgent"],
                            "task_dependencies": [["traffic_surge_task", "lock_conflict_task"]],
                            "evaluation_targets": ["active_connections_up", "lock_wait_time_up", "slow_query_count_up", "qps_down"],
                            "safety_constraints": {"max_duration_sec": 120, "max_connection_usage_ratio": 0.8, "max_cpu_usage": 90},
                            "target_chain": ["traffic_surge", "connections_up", "lock_contention", "slow_query", "qps_drop"],
                            "planner_notes": ["inferred from DBA symptom text"],
                        },
                        "used_fallback": False,
                    },
                )()

        planner = GlobalPlanner(RuntimeConfig(openai_api_key="key"), llm_client=FakeLLM())
        request = ExperimentRequest.from_dict(
            {
                "dba_description": "After promotion traffic increased, active connections spiked, many transactions waited on row locks, slow SQL increased and QPS dropped.",
                "user_constraints": {"max_duration_sec": 120},
            }
        )
        plan = planner.plan(request, self.snapshot())
        self.assertTrue(plan.llm_used)
        self.assertIn("dba_description", captured["payload"]["request"])
        self.assertIn("free-form text", captured["system"])
        self.assertEqual(plan.target_chain, ["traffic_surge", "connections_up", "lock_contention", "slow_query", "qps_drop"])

    def test_dba_description_fallback_infers_chain_without_llm(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        request = ExperimentRequest.from_dict(
            {
                "problem_description": "DBA反馈大促期间连接数暴涨，很多事务锁等待，慢查询增加，最后QPS下降。",
                "user_constraints": {"max_duration_sec": 120},
            }
        )
        plan = planner.plan(request, self.snapshot())
        self.assertFalse(plan.llm_used)
        self.assertEqual(plan.target_chain, ["traffic_surge", "connections_up", "lock_contention", "slow_query", "qps_drop"])

    def test_dba_description_does_not_match_io_inside_regular_words(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        request = ExperimentRequest.from_dict(
            {
                "dba_description": "After a promotion, connections increased, transactions waited on locks, latency rose, and QPS dropped.",
                "user_constraints": {"max_duration_sec": 120},
            }
        )
        plan = planner.plan(request, self.snapshot())
        self.assertNotIn("io", plan.target_chain)
        self.assertEqual(plan.target_chain, ["traffic_surge", "connections_up", "lock_contention", "slow_query", "qps_drop"])

    def test_agents_generate_specs_and_dag_order(self):
        config = RuntimeConfig()
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        request = ExperimentRequest(target_chain=["traffic_surge", "lock_contention", "slow_query"])
        plan = planner.plan(request, self.snapshot())
        agents = build_default_agents(config)
        outputs = []
        for root in plan.root_causes_to_inject:
            outputs.extend(agent.plan(root, plan, self.snapshot()) for agent in agents if agent.supports(root))
        dag = build_task_dag(plan, outputs)
        ordered = [task.task_id for task in topological_order(dag)]
        self.assertEqual(ordered, ["traffic_surge_task", "lock_conflict_task", "slow_sql_task"])

    def test_inspect_loop_records_no_more_than_five_react_steps(self):
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        request = ExperimentRequest(target_chain=["traffic_surge"], environment={"target_database": "db"})

        with patch("dbmags.planner.global_planner.MySQLProbe") as mysql_cls, patch("dbmags.planner.global_planner.OSProbe") as os_cls:
            mysql = mysql_cls.return_value
            mysql.version.return_value = "8.0"
            mysql.schema.return_value = {"tables": {"orders": {"columns": []}}}
            mysql.indexes.return_value = {"indexes": {}}
            mysql.constraints.return_value = {"primary_keys": {}, "foreign_keys": []}
            mysql.table_stats.return_value = {"tables": []}
            mysql.db_metrics.return_value = {"variables": {"max_connections": 100}}
            mysql.workload_probe.return_value = {"qps": 1, "tps": 1, "running": True}
            os_cls.return_value.collect.return_value = {"cpu_usage": {"percent": 1}}
            snapshot = planner.inspect(request)

        self.assertLessEqual(len(snapshot.inspect_trace), 5)
        self.assertEqual([step.action for step in snapshot.inspect_trace[:5]], ["db_version", "schema_indexes_stats", "db_metrics", "workload_probe", "os_metrics"])
        self.assertIn("constraints", snapshot.schema)


class SafetyEvaluatorTests(unittest.TestCase):
    def test_safety_rejects_dangerous_sql(self):
        snapshot = EnvironmentSnapshot(db_metrics={"variables": {"max_connections": 100}}, os_metrics={"cpu_usage": {"percent": 1}})
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        plan = planner._fallback_plan(ExperimentRequest(target_chain=["slow_query"]), snapshot, {})
        output = build_default_agents(RuntimeConfig())[2].plan("slow_query", plan, snapshot)
        output.task_spec.actions[0]["sql"] = "DROP DATABASE prod"
        dag = build_task_dag(plan, [output])
        result = SafetyChecker().check(dag, snapshot, plan.safety_constraints)
        self.assertFalse(result.approved)
        self.assertTrue(any("dangerous SQL" in reason for reason in result.reasons))

    def test_evaluator_detects_lock_and_slow_metrics(self):
        evaluator = Evaluator()
        planner = GlobalPlanner(RuntimeConfig(openai_api_key="", planner_enabled=False))
        plan = planner._fallback_plan(ExperimentRequest(target_chain=["lock_contention", "slow_query"]), EnvironmentSnapshot(), {})
        result = evaluator.evaluate(
            plan,
            ExecutableTaskDAG(tasks={}, edges=[], schedule={}),
            {"db_metrics": {"Innodb_row_lock_time": 10, "Slow_queries": 1}, "os_metrics": {}},
            {"db_metrics": {"Innodb_row_lock_time": 30, "Slow_queries": 3}, "os_metrics": {}},
            type("Trace", (), {"task_status": {}, "errors": {}, "start_time": {}})(),
            EnvironmentSnapshot(),
        )
        self.assertTrue(result.detected_events["lock_wait_time_up"])
        self.assertTrue(result.detected_events["slow_query_count_up"])

    def test_evaluator_detects_qps_down_and_latency_up(self):
        evaluator = Evaluator()
        plan = GlobalPlan(
            mode="causal_chain",
            root_causes_to_inject=["traffic_surge"],
            effects_to_observe=["qps_drop"],
            task_agents=["TrafficSurgeAgent"],
            task_dependencies=[],
            evaluation_targets=["qps_down", "p95_latency_up"],
            safety_constraints={},
        )
        result = evaluator.evaluate(
            plan,
            ExecutableTaskDAG(tasks={}, edges=[], schedule={}),
            {"db_metrics": {}, "workload_status": {"qps": 100, "p95_latency_ms": 10}, "os_metrics": {}},
            {"db_metrics": {}, "workload_status": {"qps": 70, "p95_latency_ms": 20}, "os_metrics": {}},
            ExecutionTrace(),
            EnvironmentSnapshot(),
        )
        self.assertTrue(result.detected_events["qps_down"])
        self.assertTrue(result.detected_events["p95_latency_up"])

    def test_safety_rejects_production_database(self):
        snapshot = EnvironmentSnapshot(database="production_orders", db_metrics={"variables": {"max_connections": 100}}, os_metrics={"cpu_usage": {"percent": 1}})
        dag = ExecutableTaskDAG(tasks={"t": TaskSpec("t", "slow_sql", "SlowSQLAgent", [{"kind": "sql_workload", "sql": "SELECT 1"}], {}, {})}, edges=[], schedule={})
        result = SafetyChecker().check(dag, snapshot, {"max_duration_sec": 10, "max_connection_usage_ratio": 0.8})
        self.assertFalse(result.approved)
        self.assertTrue(any("production-like" in reason for reason in result.reasons))

    def test_dag_rejects_missing_duplicate_and_cycle(self):
        plan = GlobalPlan("causal_chain", ["traffic_surge"], [], ["TrafficSurgeAgent"], [["traffic_surge_task", "missing"]], [], {})
        output = TaskSpec("traffic_surge_task", "traffic_surge", "TrafficSurgeAgent", [], {}, {})
        agent_output = type("AgentOutput", (), {"task_spec": output})()
        with self.assertRaises(ValueError):
            build_task_dag(plan, [agent_output])

        duplicate_plan = GlobalPlan("causal_chain", [], [], [], [], [], {})
        with self.assertRaises(ValueError):
            build_task_dag(duplicate_plan, [agent_output, agent_output])

        cycle_dag = ExecutableTaskDAG(
            tasks={"a": TaskSpec("a", "slow_sql", "SlowSQLAgent", [], {}, {}), "b": TaskSpec("b", "slow_sql", "SlowSQLAgent", [], {}, {})},
            edges=[TaskDAGEdge("a", "b"), TaskDAGEdge("b", "a")],
            schedule={},
        )
        with self.assertRaises(ValueError):
            topological_order(cycle_dag)


class ExecutorRuntimeTests(unittest.TestCase):
    def test_executor_waits_for_edge_condition(self):
        config = RuntimeConfig()
        executor = Executor(config)
        source = TaskSpec("a", "slow_sql", "SlowSQLAgent", [], {}, {})
        target = TaskSpec("b", "slow_sql", "SlowSQLAgent", [], {}, {}, start_policy={"condition_max_wait_sec": 1, "condition_poll_sec": 1})
        dag = ExecutableTaskDAG(tasks={"a": source, "b": target}, edges=[TaskDAGEdge("a", "b", "active_connections_up")], schedule={})

        with patch.object(executor, "_condition_satisfied", return_value=(True, {"ok": True})):
            trace = executor.execute_task_dag(dag)
        self.assertTrue(trace.condition_events["a->b"]["satisfied"])

    def test_runtime_retries_after_failed_evaluation(self):
        runtime = DBMAGSRuntime(RuntimeConfig(max_retry_rounds=2))
        tmp_memory = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_memory.cleanup)
        runtime.memory = MemoryStore(f"{tmp_memory.name}/memory.jsonl")
        snapshot = EnvironmentSnapshot(database="db", db_metrics={"variables": {"max_connections": 100}}, os_metrics={"cpu_usage": {"percent": 1}})
        plan = GlobalPlan("causal_chain", ["traffic_surge"], [], ["TrafficSurgeAgent"], [], ["active_connections_up"], {"max_duration_sec": 10, "max_connection_usage_ratio": 0.8})
        output = build_default_agents(RuntimeConfig())[0].plan("traffic_surge", plan, snapshot)

        runtime.planner.inspect = lambda request: snapshot
        runtime.planner.plan = lambda request, snap, feedback=None: plan
        runtime._task_agent_outputs = lambda gp, snap: [output]
        runtime.executor.execute_task_dag = lambda dag: ExecutionTrace(task_status={"traffic_surge_task": "completed"})
        runtime.evaluator.collect_metrics = lambda db: {"db_metrics": {}, "workload_status": {}, "os_metrics": {}}
        calls = {"count": 0}

        def fake_evaluate(*_args):
            calls["count"] += 1
            return EvaluationResult(0, 0, 1, 1, 0, 0.2 if calls["count"] == 1 else 1.0, calls["count"] == 2, failed_nodes=[] if calls["count"] == 2 else ["active_connections_up"])

        runtime.evaluator.evaluate = fake_evaluate
        result = runtime.run(ExperimentRequest(target_chain=["traffic_surge"], user_constraints={"max_retry_rounds": 2}), output_root="/tmp/dbmags_test_runs")
        self.assertEqual(len(result.rounds), 2)
        self.assertTrue(result.evaluation_result.success)

    def test_reflection_writes_long_term_memory(self):
        evaluation = EvaluationResult(0, 0, 0, 1, 0, 0.1, False, failed_nodes=["lock_wait_time_up"], causal_checks=[{"edge": "a->b", "passed": False}])
        reflection = SelfReflection().reflect(evaluation)
        self.assertTrue(any("waiter concurrency" in item for item in reflection.suggested_changes))
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(f"{tmp}/memory.jsonl")
            store.append_reflection(reflection, {"dbms": "mysql", "workload": "tpcc"})
            loaded = store.load()
        self.assertTrue(loaded["items"])

    def test_chaosblade_version_and_uid_parsing(self):
        config = RuntimeConfig(chaosblade_path="/tmp/blade")
        blade = ChaosBlade(config)

        class Proc:
            returncode = 0
            stdout = "Version: 1.8.0"
            stderr = ""

        with patch("subprocess.run", return_value=Proc()) as run:
            result = blade.version()
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(run.call_args[0][0][-1], "version")
        self.assertEqual(ChaosBlade.extract_uid('{"result":"abc"}'), "abc")


if __name__ == "__main__":
    unittest.main()
