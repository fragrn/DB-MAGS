from __future__ import annotations

from uuid import uuid4
from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec
from agent_runtime.utils import slugify


class LockConflictAgent(BaseTaskAgent):
    agent_type = "lock_conflict"
    supported_subtypes = {"record_lock", "table_lock", "metadata_lock"}

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: List[PlannedAnomaly] | None = None,
    ) -> List[TaskSpec]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        preparer = self.skills.get("prepare_lock_sql_skill")
        task_specs: List[TaskSpec] = []
        for planned_task in planned:
            column_name = planned_task.parameters.get("column_name", f"agent_meta_lock_{uuid4().hex[:8]}")
            payload = preparer.execute(
                anomaly_subtype=planned_task.anomaly_subtype,
                database=planned_task.database,
                duration_seconds=min(max(request.execution_window_seconds, 1), 10),
                column_name=str(column_name),
            )
            task_specs.append(
                TaskSpec(
                    task_id=f"lock-{slugify(planned_task.anomaly_subtype)}",
                    agent_type=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    title=payload["title"],
                    inputs={
                        "database": planned_task.database,
                        "anomaly_subtype": planned_task.anomaly_subtype,
                        "source_agent": self.agent_type,
                        "column_name": column_name,
                        **planned_task.parameters,
                    },
                    prechecks=[],
                    execution_steps=payload["execution_steps"],
                    validation_steps=[],
                    rollback_steps=payload["rollback_steps"],
                    explanation=planned_task.rationale or payload["title"],
                )
            )
        return task_specs

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
