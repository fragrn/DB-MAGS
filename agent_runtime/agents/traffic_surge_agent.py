from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec
from agent_runtime.utils import slugify


class TrafficSurgeAgent(BaseTaskAgent):
    agent_type = "traffic_surge"
    supported_subtypes = {"single_sql", "overall_workload"}

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: List[PlannedAnomaly] | None = None,
    ) -> List[TaskSpec]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        tuning_skill = self.skills.get("workload_tuning_skill")
        sql_generator = self.skills.get("generate_sql_candidate_skill")
        tasks: List[TaskSpec] = []
        for planned_task in planned:
            tuning = tuning_skill.execute(
                mode=planned_task.anomaly_subtype,
                baseline_sleep=float(planned_task.parameters.get("baseline_sleep", request.user_constraints.get("baseline_sleep", 0.044))),
                baseline_threads=int(planned_task.parameters.get("baseline_threads", request.user_constraints.get("baseline_threads", 80))),
            )
            if planned_task.anomaly_subtype == "single_sql":
                candidates = sql_generator.execute("missing_index", context)
                if candidates:
                    tuning["sql"] = candidates[0]
            tuning["database"] = planned_task.database
            tuning["duration_seconds"] = int(planned_task.parameters.get("background_duration_seconds", planned_task.parameters.get("duration_seconds", min(max(request.execution_window_seconds, 1), 20))))
            tasks.append(
                TaskSpec(
                    task_id=f"traffic-{slugify(planned_task.anomaly_subtype)}",
                    agent_type=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    title=f"Traffic surge: {planned_task.anomaly_subtype}",
                    task_role="background_anomaly",
                    inputs={
                        "database": planned_task.database,
                        "anomaly_subtype": planned_task.anomaly_subtype,
                        "source_agent": self.agent_type,
                        **tuning,
                    },
                    prechecks=[],
                    execution_steps=[{"kind": "workload_profile", **tuning}],
                    validation_steps=[],
                    rollback_steps=[],
                    explanation=planned_task.rationale or tuning["description"],
                    expected_metrics={"active_connections": "increase", "qps": "increase_initially", "latency": "increase_after_saturation"},
                    local_success_criteria={"qps_or_latency_change": True},
                    risk_assessment={"risk_level": "medium", "main_risk": "connection saturation", "confidence": 0.65},
                )
            )
        return tasks

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
