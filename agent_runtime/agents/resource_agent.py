from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, TaskSpec
from agent_runtime.utils import slugify


RESOURCE_ALIASES = {
    "resource": "cpu",
    "cpu": "cpu",
    "io": "io",
    "disk": "disk",
    "memory": "memory",
    "network": "network",
}


class ResourceAgent(BaseTaskAgent):
    agent_type = "resource"

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(self, context: DBContextSummary, request: ExperimentRequest):
        requested = [item for item in request.allowed_anomalies if item in RESOURCE_ALIASES]
        if not requested:
            return []
        resource_type = RESOURCE_ALIASES[requested[0]]
        chaos = self.skills.get("chaosblade_injection_skill")
        command = chaos.execute(resource_type=resource_type, execute=False)
        task_id = f"resource-{slugify(resource_type)}"
        return [
            TaskSpec(
                task_id=task_id,
                agent_type=self.agent_type,
                anomaly_type=resource_type,
                title=f"Resource bottleneck: {resource_type}",
                inputs={"resource_type": resource_type},
                prechecks=[],
                execution_steps=[{"kind": "shell", "command": command["command"]}],
                validation_steps=[],
                rollback_steps=[],
                explanation=f"Run ChaosBlade command to inject a {resource_type} bottleneck.",
            )
        ]

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
