from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec
from agent_runtime.utils import slugify


class ResourceBottleneckAgent(BaseTaskAgent):
    agent_type = "resource_bottleneck"
    supported_subtypes = {"cpu", "io", "network", "memory", "disk"}

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: List[PlannedAnomaly] | None = None,
    ) -> List[TaskSpec]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        chaos = self.skills.get("chaosblade_injection_skill")
        tasks: List[TaskSpec] = []
        for planned_task in planned:
            command = chaos.execute(resource_type=planned_task.anomaly_subtype, execute=False)
            tasks.append(
                TaskSpec(
                    task_id=f"resource-{slugify(planned_task.anomaly_subtype)}",
                    agent_type=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    title=f"Resource bottleneck: {planned_task.anomaly_subtype}",
                    task_role="resource_injection",
                    inputs={
                        "database": planned_task.database,
                        "anomaly_subtype": planned_task.anomaly_subtype,
                        "source_agent": self.agent_type,
                        **planned_task.parameters,
                    },
                    prechecks=[],
                    execution_steps=[{"kind": "shell", "command": command["command"], "database": planned_task.database}],
                    validation_steps=[],
                    rollback_steps=[],
                    explanation=planned_task.rationale or f"Inject {planned_task.anomaly_subtype} bottleneck via ChaosBlade.",
                    expected_metrics={f"{planned_task.anomaly_subtype}_pressure": "increase", "query_latency": "increase"},
                    local_success_criteria={"chaosblade_uid": "present", "qps": "decrease_or_latency_increase"},
                    risk_assessment={"risk_level": "medium", "main_risk": "host resource contention", "confidence": 0.65},
                )
            )
        return tasks

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
