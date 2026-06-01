from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec
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
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ) -> List[TaskAgentOutput]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        tuning_skill = self.skills.get("workload_tuning_skill")
        sql_generator = self.skills.get("generate_sql_candidate_skill")
        validator = self.skills.get("validate_sql_skill")
        explainer = self.skills.get("explain_sql_skill")
        allowed_tables = [table.name for table in context.tables]
        outputs: List[TaskAgentOutput] = []
        for planned_task in planned:
            task_input = self._input_for(planned_task.anomaly_subtype, task_inputs)
            parameters = self._merge_parameters(planned_task, task_input)
            feedback = self._feedback_text(task_input)
            react_trace = [
                self._memory_trace(task_input, planned_task.anomaly_subtype, parameters),
                self._metric_sample_trace(task_input, planned_task.anomaly_subtype),
            ]
            tuning = tuning_skill.execute(
                mode=planned_task.anomaly_subtype,
                baseline_sleep=float(parameters.get("baseline_sleep", request.user_constraints.get("baseline_sleep", 0.044))),
                baseline_threads=int(parameters.get("baseline_threads", request.user_constraints.get("baseline_threads", 80))),
            )
            if any(term in feedback for term in ("connections", "traffic", "qps", "weak", "not enough")):
                tuning["thread_count"] = self._clamp_int(parameters.get("thread_count"), int(tuning.get("thread_count", 80)) + 80, 1, 800)
                tuning["sleep_time"] = self._clamp_float(parameters.get("sleep_time"), float(tuning.get("sleep_time", 0.005)) / 2, 0.001, 1.0)
            else:
                tuning["thread_count"] = self._clamp_int(parameters.get("thread_count"), int(tuning.get("thread_count", 80)), 1, 800)
                tuning["sleep_time"] = self._clamp_float(parameters.get("sleep_time"), float(tuning.get("sleep_time", 0.005)), 0.001, 1.0)
            if planned_task.anomaly_subtype == "single_sql":
                candidates = sql_generator.execute_structured(
                    agent_name=self.agent_type,
                    anomaly_type=planned_task.anomaly_subtype,
                    subgoal=task_input.subgoal,
                    db_context=context,
                    task_input=task_input,
                    constraints={"sql_constraints": ["Traffic SQL must be safe for high-frequency concurrent execution."]},
                    candidate_count=4,
                )
                react_trace.append(
                    ReActStep(
                        thought="Generate a high-frequency SQL candidate with the task-specific LLM prompt.",
                        action="generate_sql_candidates_with_llm",
                        observation={
                            "candidate_count": len(candidates),
                            "candidates": candidates,
                            "used_static_fallback": any(item.get("source") == "static_fallback" for item in candidates if isinstance(item, dict)),
                        },
                        decision="Use the first validated high-frequency candidate for single_sql traffic pressure.",
                    )
                )
                for candidate in candidates:
                    sql = candidate.get("sql", "") if isinstance(candidate, dict) else str(candidate)
                    validation = validator.execute(sql=sql, allowed_tables=allowed_tables, anomaly_type=planned_task.anomaly_subtype)
                    react_trace.append(
                        ReActStep(
                            thought="Traffic single_sql candidates must pass safety validation before high-frequency execution.",
                            action="validate_sql_safety",
                            observation={"candidate": candidate, "validation": validation},
                            decision="Reject unsafe traffic SQL or continue to EXPLAIN.",
                            candidate_id=f"traffic:{planned_task.anomaly_subtype}:sql",
                            score=0.5 if validation.get("valid") else 0.0,
                        )
                    )
                    if not validation.get("valid"):
                        continue
                    explain = explainer.execute(validation["sql"], database=planned_task.database)
                    react_trace.append(
                        ReActStep(
                            thought="Traffic SQL should be explainable and safe to repeat.",
                            action="explain_or_probe_candidate",
                            observation={"sql": validation["sql"], "explain": explain},
                            decision="Select this SQL if EXPLAIN succeeds.",
                            candidate_id=f"traffic:{planned_task.anomaly_subtype}:sql",
                            score=0.8 if explain.get("validated") else 0.2,
                        )
                    )
                    if explain.get("validated"):
                        tuning["sql"] = validation["sql"]
                        break
            tuning["database"] = planned_task.database
            tuning["duration_seconds"] = self._clamp_int(
                parameters.get("background_duration_seconds", parameters.get("duration_seconds")),
                20 if feedback else min(max(request.execution_window_seconds, 1), 20),
                1,
                60,
            )
            max_connections = task_input.environment_snapshot.get("db_metrics", {}).get("max_connections", 0) if isinstance(task_input.environment_snapshot, dict) else 0
            react_trace.extend(
                [
                    ReActStep(
                        thought="Generate workload ramp-up candidates using planner parameters and reflexion.",
                        action="generate_traffic_candidates",
                        observation={"feedback": feedback, "candidate": tuning},
                        decision="Increase threads or lower sleep when reflexion says traffic pressure was insufficient.",
                        candidate_id=f"traffic:{planned_task.anomaly_subtype}",
                        adjustments={"thread_count": tuning.get("thread_count"), "sleep_time": tuning.get("sleep_time")},
                    ),
                    ReActStep(
                        thought="Validate connection safety before finalizing traffic surge settings.",
                        action="runtime_probe",
                        observation={"max_connections": max_connections, "thread_count": tuning.get("thread_count")},
                        decision="Finalize ramp-up profile within configured connection safety bounds.",
                        candidate_id=f"traffic:{planned_task.anomaly_subtype}",
                        score=0.65,
                    ),
                ]
            )
            task_spec = TaskSpec(
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
                    **parameters,
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
            outputs.append(
                TaskAgentOutput(
                    agent_name=self.agent_type,
                    subgoal=task_input.subgoal,
                    local_hypothesis=f"{planned_task.anomaly_subtype} should increase workload pressure during post probe.",
                    task_spec=task_spec,
                    expected_metrics=task_spec.expected_metrics,
                    local_success_criteria=task_spec.local_success_criteria,
                    risk_assessment=task_spec.risk_assessment,
                    safety_constraints=task_input.constraints,
                    cleanup_actions=task_spec.cleanup_actions,
                    fallback_plan={"baseline_sleep": tuning.get("sleep_time"), "thread_count": tuning.get("thread_count")},
                    confidence=0.65,
                    react_trace=react_trace,
                )
            )
        return outputs

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
