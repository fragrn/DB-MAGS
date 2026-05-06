from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec
from agent_runtime.utils import slugify


class SlowSQLAgent(BaseTaskAgent):
    agent_type = "slow_sql"
    supported_subtypes = {
        "missing_index",
        "excessive_index",
        "implicit_conversion",
        "multi_table_join",
        "order_by",
        "group_by",
        "large_table_scan",
    }

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: List[PlannedAnomaly] | None = None,
    ) -> List[TaskSpec]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        if not planned:
            return []
        validator = self.skills.get("validate_sql_skill")
        explainer = self.skills.get("explain_sql_skill")
        generator = self.skills.get("generate_sql_candidate_skill")
        sortscan_support = self.skills.get("prepare_sortscan_support_skill")
        implicit_support = self.skills.get("prepare_implicit_conversion_support_skill")
        excessive_support = self.skills.get("prepare_excessive_index_skill")
        task_specs: List[TaskSpec] = []
        allowed_tables = [table.name for table in context.tables]

        for planned_task in planned:
            setup_steps = []
            rollback_steps = []
            validation_steps = []
            if planned_task.anomaly_subtype in {"order_by", "group_by", "large_table_scan"}:
                support = sortscan_support.execute(database=planned_task.database)
                setup_steps.extend(support.get("setup_steps", []))
                rollback_steps.extend(support.get("rollback_steps", []))
                validation_steps.append({"kind": "support_tables", "result": {"created_tables": support.get("created_tables", [])}})
            if planned_task.anomaly_subtype == "implicit_conversion":
                support = implicit_support.execute(database=planned_task.database)
                setup_steps.extend(support.get("setup_steps", []))
                rollback_steps.extend(support.get("rollback_steps", []))
                validation_steps.append({"kind": "support_tables", "result": {"created_tables": support.get("created_tables", [])}})
            if planned_task.anomaly_subtype == "excessive_index":
                support = excessive_support.execute(database=planned_task.database)
                setup_steps.extend(support["setup_steps"])
                rollback_steps.extend(support["rollback_steps"])
                candidates = [support["query"]]
                selected_sql = support["query"]
                selected_explain = {"validated": True, "rows": [], "note": "excessive index uses pre-created support table"}
                validation_steps.insert(0, {"kind": "support_table", "result": {"table": "agent_excessive_index"}})
            else:
                candidates = generator.execute(planned_task.anomaly_subtype, context)
                selected_sql = ""
                selected_explain = {"validated": False, "error": "no candidate passed validation"}
                for candidate in candidates[:6]:
                    validation = validator.execute(sql=candidate, allowed_tables=allowed_tables + [
                        "agent_order_by_support",
                        "agent_group_by_support",
                        "agent_large_scan_support",
                        "agent_implicit_conversion_support",
                        "agent_excessive_index",
                    ], anomaly_type=planned_task.anomaly_subtype)
                    if not validation["valid"]:
                        continue
                    explain = explainer.execute(validation["sql"], database=planned_task.database)
                    if explain.get("validated"):
                        selected_sql = validation["sql"]
                        selected_explain = explain
                        validation_steps.insert(0, {"kind": "explain", "sql": validation["sql"], "result": explain})
                        break
            if not selected_sql:
                continue
            execution_steps = list(setup_steps)
            execution_steps.append(
                {
                    "kind": "sql",
                    "sql": selected_sql,
                    "database": planned_task.database,
                    "hold_seconds": min(max(request.execution_window_seconds, 1), 12),
                }
            )
            task_specs.append(
                TaskSpec(
                    task_id=f"slow-sql-{slugify(planned_task.anomaly_subtype)}",
                    agent_type=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    title=f"Slow SQL: {planned_task.anomaly_subtype}",
                    inputs={
                        "database": planned_task.database,
                        "anomaly_subtype": planned_task.anomaly_subtype,
                        "source_agent": self.agent_type,
                        "sql": selected_sql,
                        **planned_task.parameters,
                    },
                    prechecks=[],
                    execution_steps=execution_steps,
                    validation_steps=validation_steps,
                    rollback_steps=rollback_steps,
                    explanation=planned_task.rationale or f"Generate and run a {planned_task.anomaly_subtype} SQL candidate.",
                )
            )
        return task_specs

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
