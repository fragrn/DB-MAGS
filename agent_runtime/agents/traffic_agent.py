from __future__ import annotations

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, TaskSpec
from agent_runtime.utils import slugify


TRAFFIC_ALIASES = {
    "traffic": "overall_workload",
    "flow": "overall_workload",
    "single_sql": "single_sql",
    "overall_workload": "overall_workload",
}


class TrafficAgent(BaseTaskAgent):
    agent_type = "traffic"

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(self, context: DBContextSummary, request: ExperimentRequest):
        requested = [item for item in request.allowed_anomalies if item in TRAFFIC_ALIASES]
        if not requested:
            return []
        mode = TRAFFIC_ALIASES[requested[0]]
        tuning = self.skills.get("workload_tuning_skill").execute(
            mode=mode,
            baseline_sleep=float(request.user_constraints.get("baseline_sleep", 0.044)),
            baseline_threads=int(request.user_constraints.get("baseline_threads", 300)),
        )
        task_id = f"traffic-{slugify(mode)}"
        return [
            TaskSpec(
                task_id=task_id,
                agent_type=self.agent_type,
                anomaly_type=mode,
                title=f"Traffic surge: {mode}",
                inputs=tuning,
                prechecks=[],
                execution_steps=[{"kind": "workload_profile", **tuning}],
                validation_steps=[],
                rollback_steps=[],
                explanation=tuning["description"],
            )
        ]

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
