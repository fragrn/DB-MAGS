from __future__ import annotations

import json
from typing import Dict, List, Sequence

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.config import RuntimeConfig
from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import (
    DBContextSummary,
    ExperimentPlan,
    ExperimentRequest,
    PlannedAnomaly,
    PlannerDecision,
    PlannerResponse,
)


CATEGORY_TO_SUBTYPES = {
    "lock_conflict": ["record_lock", "table_lock", "metadata_lock"],
    "traffic_surge": ["single_sql", "overall_workload"],
    "slow_sql": [
        "missing_index",
        "excessive_index",
        "implicit_conversion",
        "multi_table_join",
        "order_by",
        "group_by",
        "large_table_scan",
    ],
    "resource_bottleneck": ["cpu", "io", "network", "memory", "disk"],
    "database_backup": ["database_table_backup"],
}

SUBTYPE_TO_AGENT = {
    "record_lock": "lock_conflict",
    "table_lock": "lock_conflict",
    "metadata_lock": "lock_conflict",
    "single_sql": "traffic_surge",
    "overall_workload": "traffic_surge",
    "missing_index": "slow_sql",
    "excessive_index": "slow_sql",
    "implicit_conversion": "slow_sql",
    "multi_table_join": "slow_sql",
    "order_by": "slow_sql",
    "group_by": "slow_sql",
    "large_table_scan": "slow_sql",
    "cpu": "resource_bottleneck",
    "io": "resource_bottleneck",
    "network": "resource_bottleneck",
    "memory": "resource_bottleneck",
    "disk": "resource_bottleneck",
    "database_table_backup": "database_backup",
}

LOCKISH_SUBTYPES = {"record_lock", "table_lock", "metadata_lock", "database_table_backup"}


class GlobalPlannerAgent:
    def __init__(self, config: RuntimeConfig, skills: SkillRegistry, task_agents: Sequence[BaseTaskAgent]):
        self.config = config
        self.skills = skills
        self.task_agents = list(task_agents)
        self.task_agent_map = {agent.agent_type: agent for agent in self.task_agents}
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
        planner_context = self.skills.get("build_planner_context_skill").execute(
            request_goal=request.user_goal,
            allowed_categories=self._normalize_categories(request),
            allowed_subtypes=self._normalize_subtypes(request),
            context=context,
            database_topology=request.database_topology,
        )
        planner_decision = self._planner_decision(request, planner_context)
        tasks = []
        for agent_type, agent in self.task_agent_map.items():
            planned_tasks = [item for item in planner_decision.planned_tasks if item.source_agent == agent_type]
            tasks.extend(agent.prepare(context, request, planner_tasks=planned_tasks))
        summary = planner_decision.llm_summary or self._fallback_summary(request, tasks)
        plan = ExperimentPlan(
            summary=summary,
            db_context_summary=self._describe_context(context),
            planner_decision=planner_decision,
            tasks=tasks,
            expected_signals=planner_decision.expected_signals or self._expected_signals(request),
            safety_checks=[
                "No task executes before explicit user confirmation when confirmation is enabled.",
                "SQL tasks pass static validation and EXPLAIN before execution.",
                "Task failures are isolated and cleanup is attempted per task.",
            ],
            cleanup_plan=planner_decision.cleanup_strategy or [
                "Run per-task rollback steps.",
                "Collect execution artifacts and validation evidence.",
            ],
        )
        return PlannerResponse(
            plan=plan,
            planner_decision=planner_decision,
            reasoning="LLM planner produced a structured anomaly plan.",
        )

    def revise(self, request: ExperimentRequest, revision_text: str) -> ExperimentRequest:
        request.user_constraints.setdefault("revisions", []).append(revision_text)
        request.user_goal = f"{request.user_goal}\nRevision: {revision_text}".strip()
        lowered = revision_text.lower()
        if "low" in lowered or "safer" in lowered or "reduce" in lowered:
            request.risk_level = "low"
        mentioned_subtypes = [subtype for subtype in SUBTYPE_TO_AGENT if subtype in lowered]
        mentioned_categories = [category for category in CATEGORY_TO_SUBTYPES if category in lowered]
        if "keep only" in lowered or "only " in lowered:
            request.allowed_subtypes = mentioned_subtypes
            request.anomaly_categories = mentioned_categories
            request.allowed_anomalies = list(mentioned_subtypes or mentioned_categories)
        elif "remove" in lowered or "drop" in lowered:
            request.allowed_subtypes = [item for item in request.allowed_subtypes if item not in mentioned_subtypes]
            request.anomaly_categories = [item for item in request.anomaly_categories if item not in mentioned_categories]
            request.allowed_anomalies = [item for item in request.allowed_anomalies if item not in mentioned_subtypes + mentioned_categories]
        elif "add" in lowered or "include" in lowered:
            for item in mentioned_subtypes:
                if item not in request.allowed_subtypes:
                    request.allowed_subtypes.append(item)
            for item in mentioned_categories:
                if item not in request.anomaly_categories:
                    request.anomaly_categories.append(item)
        return request

    @staticmethod
    def _missing_information(request: ExperimentRequest, context: DBContextSummary) -> List[str]:
        missing = []
        if not request.target_database:
            missing.append("Which database should I target? If you leave it blank, I can use the default TPCC database.")
        if not (request.allowed_anomalies or request.allowed_subtypes or request.anomaly_categories):
            missing.append(
                "Which anomaly categories or subtypes should I plan for? For example: slow_sql, lock_conflict, cpu, missing_index."
            )
        if not context.tables:
            missing.append("I could not read schema metadata. Please confirm the target database or provide table hints.")
        return missing

    def _planner_decision(self, request: ExperimentRequest, planner_context: Dict[str, object]) -> PlannerDecision:
        if self.llm_client.available():
            decision = self._llm_planner_decision(request, planner_context)
            if decision is not None:
                return decision
        return self._fallback_planner_decision(request)

    def _llm_planner_decision(self, request: ExperimentRequest, planner_context: Dict[str, object]) -> PlannerDecision | None:
        system_prompt = (
            "You are a MySQL anomaly planner. Return JSON only. Choose anomaly subtypes from the allow-list, "
            "assign each subtype to the correct task agent, pick the execution database, and provide concise parameters."
        )
        user_prompt = json.dumps(
            {
                "request": {
                    "goal": request.user_goal,
                    "risk_level": request.risk_level,
                    "execution_window_seconds": request.execution_window_seconds,
                    "allowed_categories": self._normalize_categories(request),
                    "allowed_subtypes": self._normalize_subtypes(request),
                    "database_topology": request.database_topology,
                    "user_constraints": request.user_constraints,
                },
                "agent_catalog": CATEGORY_TO_SUBTYPES,
                "task_agent_map": SUBTYPE_TO_AGENT,
                "planner_context": planner_context,
                "rules": {
                    "lock_and_backup_use_copy_db": True,
                    "single_sql_and_overall_workload_use_base_db": True,
                    "return_keys": [
                        "summary",
                        "selected_anomalies",
                        "task_assignments",
                        "database_mapping",
                        "task_parameters",
                        "expected_signals",
                        "cleanup_strategy",
                    ],
                },
            },
            ensure_ascii=True,
        )
        result = self.llm_client.generate_json(system_prompt, user_prompt, self.config.planner_temperature)
        if result.used_fallback or not result.text:
            return None
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            return None
        selected = [item for item in payload.get("selected_anomalies", []) if item in SUBTYPE_TO_AGENT]
        if not selected:
            return None
        assignments = {item: payload.get("task_assignments", {}).get(item, SUBTYPE_TO_AGENT[item]) for item in selected}
        assignments = {
            item: assignments[item] if assignments[item] in self.task_agent_map else SUBTYPE_TO_AGENT[item]
            for item in selected
        }
        database_mapping = {
            item: payload.get("database_mapping", {}).get(item, self._database_for_subtype(item, request.target_database))
            for item in selected
        }
        task_parameters = payload.get("task_parameters", {})
        expected_signals = payload.get("expected_signals", [])
        cleanup_strategy = payload.get("cleanup_strategy", [])
        planned_tasks = [
            PlannedAnomaly(
                anomaly_subtype=item,
                category=self._category_for_subtype(item),
                source_agent=assignments[item],
                database=database_mapping[item],
                parameters=task_parameters.get(item, {}),
                rationale=str(payload.get("rationale", f"LLM selected {item} for goal: {request.user_goal}")),
                expected_signals=expected_signals if isinstance(expected_signals, list) else [],
                cleanup_strategy=cleanup_strategy if isinstance(cleanup_strategy, list) else [],
            )
            for item in selected
        ]
        return PlannerDecision(
            selected_anomalies=selected,
            task_assignments=assignments,
            database_mapping=database_mapping,
            task_parameters={item: task_parameters.get(item, {}) for item in selected},
            expected_signals=expected_signals if isinstance(expected_signals, list) else [],
            cleanup_strategy=cleanup_strategy if isinstance(cleanup_strategy, list) else [],
            planned_tasks=planned_tasks,
            llm_summary=str(payload.get("summary", "")).strip(),
        )

    def _fallback_planner_decision(self, request: ExperimentRequest) -> PlannerDecision:
        selected = self._normalize_subtypes(request)
        if not selected:
            for category in self._normalize_categories(request):
                selected.extend(CATEGORY_TO_SUBTYPES.get(category, []))
        selected = [item for item in selected if item in SUBTYPE_TO_AGENT]
        if not selected:
            selected = ["missing_index"]
        assignments = {item: SUBTYPE_TO_AGENT[item] for item in selected}
        database_mapping = {item: self._database_for_subtype(item, request.target_database) for item in selected}
        task_parameters = {item: self._default_parameters(item, request) for item in selected}
        planned_tasks = [
            PlannedAnomaly(
                anomaly_subtype=item,
                category=self._category_for_subtype(item),
                source_agent=assignments[item],
                database=database_mapping[item],
                parameters=task_parameters[item],
                rationale=f"Fallback planner selected {item} from request constraints.",
                expected_signals=self._signals_for_subtype(item),
                cleanup_strategy=["run rollback steps", "collect metrics"],
            )
            for item in selected
        ]
        return PlannerDecision(
            selected_anomalies=selected,
            task_assignments=assignments,
            database_mapping=database_mapping,
            task_parameters=task_parameters,
            expected_signals=sorted({signal for item in selected for signal in self._signals_for_subtype(item)}),
            cleanup_strategy=["run rollback steps", "collect metrics"],
            planned_tasks=planned_tasks,
            llm_summary="",
        )

    def _normalize_categories(self, request: ExperimentRequest) -> List[str]:
        categories = list(request.anomaly_categories)
        for item in request.allowed_anomalies:
            if item in CATEGORY_TO_SUBTYPES and item not in categories:
                categories.append(item)
            elif item in SUBTYPE_TO_AGENT:
                category = self._category_for_subtype(item)
                if category not in categories:
                    categories.append(category)
        return categories

    def _normalize_subtypes(self, request: ExperimentRequest) -> List[str]:
        subtypes = list(request.allowed_subtypes)
        for item in request.allowed_anomalies:
            if item in SUBTYPE_TO_AGENT and item not in subtypes:
                subtypes.append(item)
            elif item in CATEGORY_TO_SUBTYPES:
                for subtype in CATEGORY_TO_SUBTYPES[item]:
                    if subtype not in subtypes:
                        subtypes.append(subtype)
        return subtypes

    @staticmethod
    def _category_for_subtype(subtype: str) -> str:
        for category, members in CATEGORY_TO_SUBTYPES.items():
            if subtype in members:
                return category
        return "slow_sql"

    @staticmethod
    def _database_for_subtype(subtype: str, target_database: str) -> str:
        if subtype in LOCKISH_SUBTYPES:
            if target_database.endswith("_copy"):
                return target_database
            if target_database.endswith("_base"):
                return target_database.replace("_base", "_copy")
            return f"{target_database}_copy"
        return target_database

    def _default_parameters(self, subtype: str, request: ExperimentRequest) -> Dict[str, object]:
        parameters: Dict[str, object] = {"duration_seconds": min(max(request.execution_window_seconds, 1), 20)}
        if subtype == "overall_workload":
            parameters.update(
                {
                    "baseline_sleep": request.user_constraints.get("baseline_sleep", 0.044),
                    "baseline_threads": request.user_constraints.get("baseline_threads", 80),
                }
            )
        if subtype == "single_sql":
            parameters.update(
                {
                    "baseline_sleep": request.user_constraints.get("baseline_sleep", 0.02),
                    "baseline_threads": request.user_constraints.get("baseline_threads", 12),
                }
            )
        if subtype == "database_table_backup":
            parameters.update({"source_table": request.user_constraints.get("source_table", "orders")})
        return parameters

    @staticmethod
    def _signals_for_subtype(subtype: str) -> List[str]:
        mapping = {
            "record_lock": ["row lock wait", "transaction blocking"],
            "table_lock": ["table lock wait"],
            "metadata_lock": ["metadata lock wait"],
            "single_sql": ["single query latency increase"],
            "overall_workload": ["qps increase", "latency increase"],
            "missing_index": ["full scan or skip scan", "latency increase"],
            "excessive_index": ["write amplification"],
            "implicit_conversion": ["index invalidation", "latency increase"],
            "multi_table_join": ["join explosion", "rows examined increase"],
            "order_by": ["using filesort"],
            "group_by": ["using temporary", "using filesort"],
            "large_table_scan": ["type=ALL", "rows examined increase"],
            "cpu": ["cpu saturation"],
            "io": ["io wait increase"],
            "network": ["network delay"],
            "memory": ["memory pressure"],
            "disk": ["disk pressure"],
            "database_table_backup": ["backup table created"],
        }
        return mapping.get(subtype, ["task-specific validation evidence"])

    @staticmethod
    def _fallback_summary(request: ExperimentRequest, tasks) -> str:
        anomalies = ", ".join(task.anomaly_type for task in tasks) or "unspecified anomalies"
        return (
            f"Prepare {len(tasks)} task(s) for {anomalies} against {request.target_database or 'the default database'} "
            f"with risk level {request.risk_level}."
        )

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
        if any(item in request.allowed_anomalies for item in ("cpu", "resource_bottleneck")):
            signals.append("system CPU saturation")
        if any(item in request.allowed_anomalies for item in ("traffic_surge", "overall_workload", "single_sql")):
            signals.append("workload concurrency increase")
        return signals
