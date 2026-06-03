from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec
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
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ) -> List[TaskAgentOutput]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        chaos = self.skills.get("chaosblade_injection_skill")
        outputs: List[TaskAgentOutput] = []
        for planned_task in planned:
            task_input = self._input_for(planned_task.anomaly_subtype, task_inputs)
            parameters = self._merge_parameters(planned_task, task_input)
            feedback = self._feedback_text(task_input)
            os_metrics = task_input.environment_snapshot.get("os_metrics", {})
            default_duration = 20 if any(term in feedback for term in ("weak", "not enough", "latency", "qps", "pressure")) else 12
            cpu_percent = float(os_metrics.get("cpu_percent", 0) or 0) if isinstance(os_metrics, dict) else 0.0
            intensity = str(parameters.get("intensity") or ("medium" if cpu_percent > 75 else "high" if feedback else "default"))
            duration = self._clamp_int(parameters.get("duration_seconds", parameters.get("background_duration_seconds")), default_duration, 1, 60)
            llm_resource_candidates = []
            if "generate_resource_candidate_skill" in self.skills.list_names():
                llm_resource_candidates = self.skills.get("generate_resource_candidate_skill").execute(
                    anomaly_type=planned_task.anomaly_subtype,
                    task_input=task_input,
                    os_metrics=os_metrics if isinstance(os_metrics, dict) else {},
                    parameters=parameters,
                )
            if llm_resource_candidates:
                chosen = llm_resource_candidates[0]
                intensity = str(chosen.get("intensity") or intensity)
                duration = self._clamp_int(chosen.get("duration_seconds"), duration, 1, 60)
            react_trace = [
                self._memory_trace(task_input, planned_task.anomaly_subtype, parameters),
                self._metric_sample_trace(task_input, planned_task.anomaly_subtype),
                ReActStep(
                    thought="Generate resource pressure candidate from the resource-specific prompt, OS snapshot, and reflexion.",
                    action="generate_resource_candidates",
                    observation={"os_metrics": os_metrics, "requested_resource": planned_task.anomaly_subtype, "llm_candidates": llm_resource_candidates},
                    decision=f"Choose {intensity} intensity for {duration}s.",
                    candidate_id=f"resource:{planned_task.anomaly_subtype}",
                    adjustments={"intensity": intensity, "duration_seconds": duration},
                ),
                ReActStep(
                    thought="Validate current resource headroom before finalizing the pressure task.",
                    action="runtime_probe",
                    observation={"cpu_percent": cpu_percent, "os_metrics": os_metrics},
                    decision="Downgrade intensity if host resources are already near the boundary; otherwise keep stronger pressure.",
                    candidate_id=f"resource:{planned_task.anomaly_subtype}",
                    score=0.55 if cpu_percent > 75 else 0.75,
                ),
            ]
            command = chaos.execute(resource_type=planned_task.anomaly_subtype, execute=False)
            task_spec = TaskSpec(
                task_id=f"resource-{slugify(planned_task.anomaly_subtype)}",
                agent_type=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                title=f"Resource bottleneck: {planned_task.anomaly_subtype}",
                task_role="resource_injection",
                inputs={
                    "database": planned_task.database,
                    "anomaly_subtype": planned_task.anomaly_subtype,
                    "source_agent": self.agent_type,
                    "duration_seconds": duration,
                    "intensity": intensity,
                    **parameters,
                },
                prechecks=[],
                execution_steps=[{"kind": "shell", "command": command["command"], "database": planned_task.database, "duration_seconds": duration, "intensity": intensity}],
                validation_steps=[],
                rollback_steps=[],
                explanation=planned_task.rationale or f"Inject {planned_task.anomaly_subtype} bottleneck via ChaosBlade.",
                expected_metrics={f"{planned_task.anomaly_subtype}_pressure": "increase", "query_latency": "increase"},
                local_success_criteria={"chaosblade_uid": "present", "qps": "decrease_or_latency_increase"},
                risk_assessment={"risk_level": "medium", "main_risk": "host resource contention", "confidence": 0.65},
            )
            outputs.append(
                TaskAgentOutput(
                    agent_name=self.agent_type,
                    subgoal=task_input.subgoal,
                    local_hypothesis=f"{planned_task.anomaly_subtype} pressure should reduce probe capacity or increase latency.",
                    task_spec=task_spec,
                    expected_metrics=task_spec.expected_metrics,
                    local_success_criteria=task_spec.local_success_criteria,
                    risk_assessment=task_spec.risk_assessment,
                    safety_constraints=task_input.constraints,
                    cleanup_actions=task_spec.cleanup_actions,
                    fallback_plan={"chaosblade": command.get("command", "")},
                    confidence=0.65,
                    react_trace=react_trace,
                )
            )
        return outputs

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
