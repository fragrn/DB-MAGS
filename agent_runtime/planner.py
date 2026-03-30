from __future__ import annotations

import json
from dataclasses import asdict
from typing import List, Sequence

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.config import RuntimeConfig
from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentPlan, ExperimentRequest, PlannerResponse


class GlobalPlannerAgent:
    def __init__(self, config: RuntimeConfig, skills: SkillRegistry, task_agents: Sequence[BaseTaskAgent]):
        self.config = config
        self.skills = skills
        self.task_agents = list(task_agents)
        self.llm_client = ResponsesAPIClient(config)

    def gather_context(self, request: ExperimentRequest) -> DBContextSummary:
        database = request.target_database or self.config.default_database
        context = self.skills.get("inspect_schema_skill").execute(database=database)
        context.distribution = self.skills.get("inspect_distribution_skill").execute(database=database)
        return context

    def plan(self, request: ExperimentRequest, context: DBContextSummary) -> PlannerResponse:
        missing = self._missing_information(request, context)
        if missing:
            return PlannerResponse(follow_up_questions=missing, reasoning="Need more information before planning.")
        llm_summary = self._llm_plan_summary(request, context)
        tasks = []
        for agent in self.task_agents:
            tasks.extend(agent.prepare(context, request))
        summary = llm_summary or self._fallback_summary(request, tasks)
        plan = ExperimentPlan(
            summary=summary,
            db_context_summary=self._describe_context(context),
            tasks=tasks,
            expected_signals=self._expected_signals(request),
            safety_checks=[
                "No task executes before explicit user confirmation.",
                "SQL tasks pass static validation and EXPLAIN before execution.",
                "Task failures are isolated and cleanup is attempted per task.",
            ],
            cleanup_plan=["Run per-task rollback steps.", "Collect execution artifacts and validation evidence."],
        )
        return PlannerResponse(plan=plan, reasoning="Plan generated from schema context and allowed anomalies.")

    def revise(self, request: ExperimentRequest, revision_text: str) -> ExperimentRequest:
        lowered = revision_text.lower()
        if "lower" in lowered or "safer" in lowered:
            request.risk_level = "low"
        if "cpu" in lowered and "cpu" not in request.allowed_anomalies:
            request.allowed_anomalies.append("cpu")
        if "index" in lowered and "missing_index" not in request.allowed_anomalies:
            request.allowed_anomalies.append("missing_index")
        request.user_constraints["revision_note"] = revision_text
        request.user_goal = f"{request.user_goal}\nRevision: {revision_text}".strip()
        return request

    @staticmethod
    def _missing_information(request: ExperimentRequest, context: DBContextSummary) -> List[str]:
        missing = []
        if not request.target_database:
            missing.append("Which database should I target? If you leave it blank, I can use the default tpcc10_test.")
        if not request.allowed_anomalies:
            missing.append("Which anomaly types should I plan for? For example: missing_index, cpu, overall_workload.")
        if not context.tables:
            missing.append("I could not read schema metadata. Please confirm the target database or provide table hints.")
        return missing

    def _llm_plan_summary(self, request: ExperimentRequest, context: DBContextSummary) -> str:
        if not self.llm_client.available():
            return ""
        prompt = json.dumps(
            {
                "goal": request.user_goal,
                "anomalies": request.allowed_anomalies,
                "risk_level": request.risk_level,
                "database": context.database,
                "tables": [table.name for table in context.tables[:8]],
            },
            ensure_ascii=True,
        )
        result = self.llm_client.generate_json(
            system_prompt="Return JSON with one key named summary. The summary should be concise and operational.",
            user_prompt=prompt,
            temperature=self.config.planner_temperature,
        )
        if result.used_fallback or not result.text:
            return ""
        try:
            return json.loads(result.text).get("summary", "")
        except json.JSONDecodeError:
            return ""

    @staticmethod
    def _fallback_summary(request: ExperimentRequest, tasks) -> str:
        anomalies = ", ".join(request.allowed_anomalies) or "unspecified anomalies"
        return f"Prepare {len(tasks)} task(s) for {anomalies} against {request.target_database or 'the default database'} with risk level {request.risk_level}."

    @staticmethod
    def _describe_context(context: DBContextSummary) -> str:
        if not context.tables:
            return "; ".join(context.notes) or "No schema metadata available."
        largest = sorted(context.tables, key=lambda table: table.row_count or 0, reverse=True)[:5]
        parts = [f"{table.name}(rows={table.row_count or 'unknown'}, indexes={len(table.indexes)})" for table in largest]
        if context.notes:
            parts.extend(context.notes)
        return "; ".join(parts)

    @staticmethod
    def _expected_signals(request: ExperimentRequest) -> List[str]:
        signals = ["QPS change", "latency increase", "task-specific validation evidence"]
        if any(item in request.allowed_anomalies for item in ("cpu", "resource")):
            signals.append("system CPU saturation")
        if any(item in request.allowed_anomalies for item in ("traffic", "flow", "overall_workload")):
            signals.append("workload concurrency increase")
        return signals
