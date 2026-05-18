from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec
from agent_runtime.utils import slugify


class BackupAgent(BaseTaskAgent):
    agent_type = "database_backup"
    supported_subtypes = {"database_table_backup"}

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: List[PlannedAnomaly] | None = None,
    ) -> List[TaskSpec]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        preparer = self.skills.get("prepare_backup_task_skill")
        tasks: List[TaskSpec] = []
        for planned_task in planned:
            source_table = str(planned_task.parameters.get("source_table", "orders"))
            backup_table = str(planned_task.parameters.get("backup_table", f"{source_table}_backup_agent"))
            payload = preparer.execute(database=planned_task.database, source_table=source_table, backup_table=backup_table)
            tasks.append(
                TaskSpec(
                    task_id=f"backup-{slugify(source_table)}",
                    agent_type=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    title=payload["title"],
                    task_role="backup_job",
                    inputs={
                        "database": planned_task.database,
                        "anomaly_subtype": planned_task.anomaly_subtype,
                        "source_agent": self.agent_type,
                        **planned_task.parameters,
                    },
                    prechecks=[],
                    execution_steps=payload["execution_steps"],
                    validation_steps=[],
                    rollback_steps=payload["rollback_steps"],
                    explanation=planned_task.rationale or payload["title"],
                )
            )
        return tasks

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
