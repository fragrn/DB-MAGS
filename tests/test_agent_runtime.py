from __future__ import annotations

from pathlib import Path

import unittest
from unittest.mock import patch

from agent_runtime.conversation import CLIConversationOrchestrator
from agent_runtime.executor import TaskExecutor
from agent_runtime.planner import GlobalPlannerAgent
from agent_runtime.runtime import build_runtime
from agent_runtime.scheduler import TaskScheduler
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.skills.base import Skill
from agent_runtime.types import (
    DBContextSummary,
    DBTableProfile,
    ExperimentPlan,
    ExperimentRequest,
    PlannerResponse,
    TaskResult,
    TaskSpec,
)


class DummySkill(Skill):
    name = "dummy"

    def __init__(self, payload=None):
        self.payload = payload or {}

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
            skills=SkillRegistry([]),
            task_agents=[],
        )
        planner.llm_client = type("LLM", (), {"available": lambda self: False})()
        request = ExperimentRequest(user_goal="need a test")
        context = DBContextSummary(database="", tables=[])
        response = planner.plan(request, context)
        self.assertTrue(response.follow_up_questions)
        self.assertIsNone(response.plan)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_isolates_failures(self):
        skills = SkillRegistry([FakeRunnerSkill(), FakeMetricsSkill(), FakeCleanupSkill()])
        executor = TaskExecutor(skills)
        scheduler = TaskScheduler(executor, max_workers=2)
        results = scheduler.run(
            [
                TaskSpec(task_id="ok", agent_type="sql", anomaly_type="missing_index", title="ok", execution_steps=[{"kind": "sql"}]),
                TaskSpec(task_id="bad", agent_type="sql", anomaly_type="missing_index", title="bad", execution_steps=[{"kind": "fail"}]),
            ]
        )
        by_id = {item.task_id: item for item in results}
        self.assertEqual(by_id["ok"].status, "completed")
        self.assertEqual(by_id["bad"].status, "failed")


class ConversationTests(unittest.TestCase):
    def test_confirm_runs_tasks_only_after_plan_display(self):
        plan = ExperimentPlan(
            summary="test plan",
            db_context_summary="db",
            tasks=[TaskSpec(task_id="task-1", agent_type="sql", anomaly_type="missing_index", title="title")],
        )

        class FakePlanner:
            def __init__(self):
                self.calls = 0

            def gather_context(self, request):
                return DBContextSummary(database="tpcc10_test", tables=[DBTableProfile(name="orders")])

            def plan(self, request, context):
                self.calls += 1
                return PlannerResponse(plan=plan)

            def revise(self, request, revision_text):
                request.user_goal = revision_text
                return request

        class FakeScheduler:
            def run(self, tasks):
                return [TaskResult(task_id="task-1", status="completed")]

        orchestrator = CLIConversationOrchestrator(planner=FakePlanner(), scheduler=FakeScheduler())
        with patch("builtins.input", side_effect=["show plan", "confirm"]), patch("builtins.print"):
            result = orchestrator.run(ExperimentRequest(user_goal="goal", target_database="tpcc10_test", allowed_anomalies=["missing_index"]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task_results[0].task_id, "task-1")


class RuntimeSmokeTests(unittest.TestCase):
    def test_runtime_builds(self):
        runtime = build_runtime()
        self.assertIsNotNone(runtime)




class ExperimentValidationTests(unittest.TestCase):
    def test_validation_runner_writes_structured_results_without_openai(self):
        import tempfile
        from agent_runtime.experiment_validation import AgentValidationRunner
        from agent_runtime.runtime import build_components
        from agent_runtime.config import RuntimeConfig

        config = RuntimeConfig.from_env()
        config.openai_api_key = ""
        config.planner_enabled = False
        runner = AgentValidationRunner(build_components(config))
        with tempfile.TemporaryDirectory() as tmpdir:
            suite = runner.run(output_root=Path(tmpdir), database="tpcc10_test")
            self.assertEqual(len(suite["experiments"]), 5)
            sql_result = [item for item in suite["experiments"] if item["agent"] == "SQLAnomalyAgent"][0]
            self.assertEqual(sql_result["status"], "fail")
            self.assertIn("OpenAI", sql_result["failure_reason"])

if __name__ == "__main__":
    unittest.main()
