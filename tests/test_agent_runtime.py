from __future__ import annotations

from pathlib import Path

import io
import json
import urllib.error
import unittest
from unittest.mock import patch

from agent_runtime.conversation import CLIConversationOrchestrator
from agent_runtime.executor import TaskExecutor
from agent_runtime.experiment_validation import AgentValidationRunner
from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skills.injection_bridge import RunInjectionSkill
from agent_runtime.planner import GlobalPlannerAgent
from agent_runtime.runtime import build_runtime
from agent_runtime.scheduler import TaskScheduler
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.skills.base import Skill
from scripts.run_ready_experiments import compare_metrics
from agent_runtime.types import (
    DBContextSummary,
    DBTableProfile,
    ExperimentPlan,
    ExperimentRequest,
    PlannedAnomaly,
    PlannerDecision,
    PlannerResponse,
    TaskResult,
    TaskSpec,
)


class DummySkill(Skill):
    name = "dummy"

    def __init__(self, payload=None, name: str | None = None):
        self.payload = payload or {}
        if name:
            self.name = name

    def execute(self, **kwargs):
        return self.payload


class FakeRunnerSkill(Skill):
    name = "run_injection_skill"

    def execute(self, step):
        if step.get("kind") == "fail":
            return {"executed": False, "error": "boom"}
        return {"executed": True, "step": step}


class FakeMetricsSkill(Skill):
    name = "collect_metrics_skill"

    def execute(self, task_id, anomaly_type, artifacts):
        return {"signals": [task_id, anomaly_type, str(bool(artifacts.get("executed")))]}


class FakeCleanupSkill(Skill):
    name = "cleanup_skill"

    def execute(self, rollback_steps, runner):
        return {"cleaned": True, "results": []}


class PlannerTests(unittest.TestCase):
    def test_planner_asks_follow_up_when_request_is_missing_inputs(self):
        planner = GlobalPlannerAgent(
            config=type("Config", (), {"default_database": "tpcc10_test", "planner_temperature": 0.0})(),
            skills=SkillRegistry([DummySkill(payload={}, name="build_planner_context_skill")]),
            task_agents=[],
        )
        planner.llm_client = type("LLM", (), {"available": lambda self: False})()
        request = ExperimentRequest(user_goal="need a test")
        context = DBContextSummary(database="", tables=[])
        response = planner.plan(request, context)
        self.assertTrue(response.follow_up_questions)
        self.assertIsNone(response.plan)

    def test_fallback_planner_builds_structured_decision(self):
        planner = GlobalPlannerAgent(
            config=type("Config", (), {"default_database": "tpcc10_test", "planner_temperature": 0.0})(),
            skills=SkillRegistry([DummySkill(payload={"tables": []}, name="build_planner_context_skill")]),
            task_agents=[],
        )
        planner.llm_client = type("LLM", (), {"available": lambda self: False})()
        request = ExperimentRequest(
            user_goal="run missing index and cpu",
            target_database="dbmags_tpcc_base",
            allowed_subtypes=["missing_index", "cpu"],
            anomaly_categories=["slow_sql", "resource_bottleneck"],
        )
        context = DBContextSummary(database="dbmags_tpcc_base", tables=[DBTableProfile(name="orders", row_count=1000)])
        response = planner.plan(request, context)
        self.assertIsNotNone(response.plan)
        self.assertEqual(sorted(response.planner_decision.selected_anomalies), ["cpu", "missing_index"])

    def test_planner_records_llm_fallback_error_details(self):
        planner = GlobalPlannerAgent(
            config=type("Config", (), {"default_database": "tpcc10_test", "planner_temperature": 0.0})(),
            skills=SkillRegistry([DummySkill(payload={"tables": []}, name="build_planner_context_skill")]),
            task_agents=[],
        )

        class FakeLLMResult:
            used_fallback = True
            text = ""
            error_type = "http_error"
            error_message = "Unsupported parameter: 'temperature' is not supported with this model."
            transport_used = "responses"

        planner.llm_client = type(
            "LLM",
            (),
            {
                "available": lambda self: True,
                "generate_json": lambda self, *_args, **_kwargs: FakeLLMResult(),
            },
        )()
        request = ExperimentRequest(
            user_goal="auto mix",
            target_database="dbmags_tpcc_base",
            mode="multi_auto",
        )
        context = DBContextSummary(database="dbmags_tpcc_base", tables=[DBTableProfile(name="orders", row_count=1000)])
        response = planner.plan(request, context)
        self.assertIn("temperature", response.planner_decision.llm_error)
        self.assertEqual(response.planner_decision.llm_error_type, "http_error")
        self.assertEqual(response.planner_decision.llm_transport, "responses")

    def test_parse_selected_anomalies_accepts_dict_entries(self):
        raw = [
            {"anomaly_subtype": "missing_index"},
            {"subtype": "record_lock"},
            "overall_workload",
            "traffic_surge",
        ]
        chosen = GlobalPlannerAgent._parse_selected_anomalies(raw)
        self.assertEqual(chosen, ["missing_index", "record_lock", "overall_workload"])


class LLMClientTests(unittest.TestCase):
    def test_default_mode_uses_chat_completions(self):
        config = type(
            "Config",
            (),
            {
                "openai_api_key": "test-key",
                "openai_base_url": "https://api.openai.com/v1",
                "openai_model": "gpt-4o",
                "openai_api_mode": "chat_completions",
                "planner_enabled": True,
            },
        )()
        client = ResponsesAPIClient(config)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}]}).encode("utf-8")

        captured = {}

        def fake_urlopen(req, timeout=60):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", "user", 0.2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.transport_used, "chat_completions")
        self.assertIn("messages", captured["payload"])
        self.assertEqual(captured["payload"]["messages"][0]["role"], "system")
        self.assertEqual(captured["payload"]["messages"][1]["role"], "user")

    def test_chat_completion_strips_json_fences(self):
        config = type(
            "Config",
            (),
            {
                "openai_api_key": "test-key",
                "openai_base_url": "https://api.vectorengine.cn/v1",
                "openai_model": "gpt-4o",
                "openai_api_mode": "chat_completions",
                "planner_enabled": True,
            },
        )()
        client = ResponsesAPIClient(config)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}]}).encode("utf-8")

        with patch("urllib.request.urlopen", lambda req, timeout=60: FakeResponse()):
            result = client.generate_json("system", "user", 0.2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.text, "{\"ok\": true}")

    def test_gpt5_request_omits_temperature_for_responses(self):
        config = type(
            "Config",
            (),
            {
                "openai_api_key": "test-key",
                "openai_base_url": "https://api.openai.com/v1",
                "openai_model": "gpt-5",
                "openai_api_mode": "responses",
                "planner_enabled": True,
            },
        )()
        client = ResponsesAPIClient(config)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"output_text": "{\"ok\":true}"}).encode("utf-8")

        captured = {}

        def fake_urlopen(req, timeout=60):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", "user", 0.2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.transport_used, "responses")
        self.assertNotIn("temperature", captured["payload"])

    def test_client_retries_without_temperature_on_unsupported_parameter_error(self):
        config = type(
            "Config",
            (),
            {
                "openai_api_key": "test-key",
                "openai_base_url": "https://api.openai.com/v1",
                "openai_model": "custom-model",
                "openai_api_mode": "responses",
                "planner_enabled": True,
            },
        )()
        client = ResponsesAPIClient(config)

        attempts = {"count": 0}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"output_text": "{\"ok\":true}"}).encode("utf-8")

        def fake_urlopen(req, timeout=60):
            payload = json.loads(req.data.decode("utf-8"))
            attempts["count"] += 1
            if attempts["count"] == 1:
                self.assertIn("temperature", payload)
                body = json.dumps({"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}}).encode("utf-8")
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(body))
            self.assertNotIn("temperature", payload)
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", "user", 0.2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(result.transport_used, "responses")

    def test_auto_mode_falls_back_from_chat_to_responses(self):
        config = type(
            "Config",
            (),
            {
                "openai_api_key": "test-key",
                "openai_base_url": "https://api.vectorengine.cn/v1",
                "openai_model": "gpt-4o",
                "openai_api_mode": "auto",
                "planner_enabled": True,
            },
        )()
        client = ResponsesAPIClient(config)

        calls = []

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.body).encode("utf-8")

        def fake_urlopen(req, timeout=60):
            calls.append(req.full_url)
            payload = json.loads(req.data.decode("utf-8"))
            if req.full_url.endswith("/chat/completions"):
                body = json.dumps({"error": {"message": "当前分组上游负载已饱和"}}).encode("utf-8")
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(body))
            self.assertIn("input", payload)
            return FakeResponse({"output_text": "{\"ok\":true}"})

        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.generate_json("system", "user", 0.2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.transport_used, "responses")
        self.assertEqual(calls, ["https://api.vectorengine.cn/v1/chat/completions", "https://api.vectorengine.cn/v1/responses"])


class SchedulerTests(unittest.TestCase):
    def test_scheduler_isolates_failures(self):
        skills = SkillRegistry([FakeRunnerSkill(), FakeMetricsSkill(), FakeCleanupSkill()])
        executor = TaskExecutor(skills)
        scheduler = TaskScheduler(executor, max_workers=2)
        results = scheduler.run(
            [
                TaskSpec(task_id="ok", agent_type="slow_sql", anomaly_type="missing_index", title="ok", execution_steps=[{"kind": "sql"}]),
                TaskSpec(task_id="bad", agent_type="slow_sql", anomaly_type="missing_index", title="bad", execution_steps=[{"kind": "fail"}]),
            ]
        )
        by_id = {item.task_id: item for item in results}
        self.assertEqual(by_id["ok"].status, "completed")
        self.assertEqual(by_id["bad"].status, "failed")


class ComparisonTests(unittest.TestCase):
    def test_compare_metrics_confirms_single_sql_when_qps_and_latency_rise(self):
        comparison = compare_metrics(
            {"subtype": "single_sql"},
            {
                "qps": 100.0,
                "avg_latency_ms": 5.0,
                "p95_latency_ms": 8.0,
                "failed_transactions": 0,
            },
            {
                "qps": 140.0,
                "avg_latency_ms": 8.0,
                "p95_latency_ms": 12.0,
                "failed_transactions": 0,
            },
        )
        self.assertTrue(comparison["anomaly_confirmed"])

    def test_compare_metrics_confirms_excessive_index_when_update_slows_down(self):
        comparison = compare_metrics(
            {"subtype": "excessive_index"},
            {"avg_latency_ms": 10.0, "failed_transactions": 0, "db_evidence": {}},
            {"avg_latency_ms": 18.0, "failed_transactions": 0, "db_evidence": {"redundant_indexes_present": True}},
        )
        self.assertTrue(comparison["anomaly_confirmed"])

    def test_compare_metrics_marks_network_as_environment_blocked(self):
        comparison = compare_metrics(
            {"subtype": "network"},
            {"qps": 10.0, "p95_latency_ms": 10.0, "failed_transactions": 0},
            {"qps": 10.0, "p95_latency_ms": 10.0, "failed_transactions": 0, "db_evidence": {"chaosblade": {"executed": False}}},
        )
        self.assertEqual(comparison["comparison_status"], "environment_blocked")
        self.assertFalse(comparison["anomaly_confirmed"])

    def test_compare_metrics_confirms_table_lock_when_probe_waits(self):
        comparison = compare_metrics(
            {"subtype": "table_lock"},
            {"single_sql_mean_ms": 15.0, "failed_transactions": 0},
            {"single_sql_mean_ms": 1500.0, "failed_transactions": 0, "db_evidence": {"lock_holder": {"executed": True}}},
        )
        self.assertTrue(comparison["anomaly_confirmed"])

    def test_compare_metrics_confirms_backup_when_row_counts_match(self):
        comparison = compare_metrics(
            {"subtype": "database_table_backup"},
            {"qps": 20.0, "p95_latency_ms": 10.0, "failed_transactions": 0},
            {
                "qps": 18.0,
                "p95_latency_ms": 12.0,
                "failed_transactions": 0,
                "db_evidence": {"backup": {"backup_row_count": 100, "source_row_count": 100}},
            },
        )
        self.assertTrue(comparison["anomaly_confirmed"])

    def test_run_sql_probe_path_records_latency(self):
        class DummyCursor:
            def execute(self, _sql):
                return None

            def close(self):
                return None

        class DummyConn:
            def commit(self):
                return None

            def close(self):
                return None

        from contextlib import contextmanager

        @contextmanager
        def fake_db_cursor(*args, **kwargs):
            yield DummyConn(), DummyCursor()

        runner = RunInjectionSkill()
        with patch("agent_runtime.skills.injection_bridge.db_cursor", fake_db_cursor):
            result = runner.execute({"kind": "sql", "sql": "SELECT 1", "database": "dbmags_tpcc_base"})
        self.assertTrue(result["executed"])
        self.assertIn("latency_ms", result)
        self.assertIn("elapsed_seconds", result)


class ConversationTests(unittest.TestCase):
    def test_confirm_runs_tasks_only_after_plan_display(self):
        plan = ExperimentPlan(
            summary="test plan",
            db_context_summary="db",
            planner_decision=PlannerDecision(selected_anomalies=["missing_index"]),
            tasks=[TaskSpec(task_id="task-1", agent_type="slow_sql", anomaly_type="missing_index", title="title")],
        )

        class FakePlanner:
            def gather_context(self, request):
                return DBContextSummary(database="tpcc10_test", tables=[DBTableProfile(name="orders")])

            def plan(self, request, context):
                return PlannerResponse(plan=plan, planner_decision=plan.planner_decision)

            def revise(self, request, revision_text):
                request.user_goal = revision_text
                return request

        class FakeScheduler:
            def run(self, tasks):
                return [TaskResult(task_id="task-1", status="completed")]

        orchestrator = CLIConversationOrchestrator(planner=FakePlanner(), scheduler=FakeScheduler())
        with patch("builtins.input", side_effect=["show plan", "confirm"]), patch("builtins.print"):
            result = orchestrator.run(ExperimentRequest(user_goal="goal", target_database="tpcc10_test", allowed_subtypes=["missing_index"]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task_results[0].task_id, "task-1")


class RuntimeSmokeTests(unittest.TestCase):
    def test_runtime_builds(self):
        runtime = build_runtime()
        self.assertIsNotNone(runtime)


class ExperimentValidationTests(unittest.TestCase):
    def test_validation_runner_writes_structured_results_without_openai(self):
        import tempfile

        class FakeValidationPlanner:
            def gather_context(self, request):
                return DBContextSummary(database=request.target_database or "dbmags_tpcc_base", tables=[DBTableProfile(name="orders", row_count=1000)])

            def plan(self, request, context):
                if not request.target_database and not request.allowed_subtypes and not request.anomaly_categories:
                    return PlannerResponse(follow_up_questions=["Which database should I target?"])
                planned_tasks = []
                for subtype in request.allowed_subtypes:
                    agent_type = {
                        "missing_index": "slow_sql",
                        "cpu": "resource_bottleneck",
                        "overall_workload": "traffic_surge",
                        "record_lock": "lock_conflict",
                        "database_table_backup": "database_backup",
                    }[subtype]
                    planned_tasks.append(
                        PlannedAnomaly(
                            anomaly_subtype=subtype,
                            category=request.anomaly_categories[0],
                            source_agent=agent_type,
                            database=request.target_database or "dbmags_tpcc_base",
                        )
                    )
                tasks = [
                    TaskSpec(
                        task_id=f"{item.source_agent}-{item.anomaly_subtype}",
                        agent_type=item.source_agent,
                        anomaly_type=item.anomaly_subtype,
                        title=item.anomaly_subtype,
                    )
                    for item in planned_tasks
                ]
                decision = PlannerDecision(
                    selected_anomalies=[item.anomaly_subtype for item in planned_tasks],
                    planned_tasks=planned_tasks,
                )
                plan = ExperimentPlan(
                    summary="ok",
                    db_context_summary="orders",
                    tasks=tasks,
                    planner_decision=decision,
                )
                return PlannerResponse(plan=plan, planner_decision=decision)

            def revise(self, request, revision_text):
                request.allowed_subtypes = ["missing_index"]
                request.anomaly_categories = ["slow_sql"]
                return request

        class FakeValidationComponents:
            def __init__(self):
                self.config = type("Config", (), {"openai_model": "gpt-5"})()
                self.llm_client = type("LLM", (), {"available": lambda self: False})()
                self.planner = FakeValidationPlanner()

        runner = AgentValidationRunner(FakeValidationComponents())
        with tempfile.TemporaryDirectory() as tmpdir:
            suite = runner.run(output_root=Path(tmpdir), database="tpcc10_test")
            self.assertEqual(len(suite["experiments"]), 7)
            global_result = [item for item in suite["experiments"] if item["agent"] == "GlobalPlannerAgent"][0]
            self.assertEqual(global_result["status"], "pass")


if __name__ == "__main__":
    unittest.main()
