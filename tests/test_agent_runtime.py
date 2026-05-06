from __future__ import annotations

from pathlib import Path

import unittest
from unittest.mock import patch

from agent_runtime.conversation import CLIConversationOrchestrator
from agent_runtime.executor import TaskExecutor
from agent_runtime.experiment_validation import AgentValidationRunner
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
