from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec
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
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ) -> List[TaskAgentOutput]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        if not planned:
            return []
        validator = self.skills.get("validate_sql_skill")
        explainer = self.skills.get("explain_sql_skill")
        generator = self.skills.get("generate_sql_candidate_skill")
        sortscan_support = self.skills.get("prepare_sortscan_support_skill")
        implicit_support = self.skills.get("prepare_implicit_conversion_support_skill")
        excessive_support = self.skills.get("prepare_excessive_index_skill")
        outputs: List[TaskAgentOutput] = []
        allowed_tables = [table.name for table in context.tables]

        for planned_task in planned:
            task_input = self._input_for(planned_task.anomaly_subtype, task_inputs)
            parameters = self._merge_parameters(planned_task, task_input)
            feedback = self._feedback_text(task_input)
            react_trace = [
                self._memory_trace(task_input, planned_task.anomaly_subtype, parameters),
                self._metric_sample_trace(task_input, planned_task.anomaly_subtype),
            ]
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
                selected_sql = support["query"]
                validation_steps.insert(0, {"kind": "support_table", "result": {"table": "agent_excessive_index"}})
                react_trace.append(
                    ReActStep(
                        thought="Use prepared excessive-index support query as the candidate task.",
                        action="generate_slow_sql_candidates",
                        observation={"candidate_count": 1, "sql": selected_sql},
                        decision="Support query is selected because setup creates the intended index overhead.",
                        candidate_id="slow_sql:excessive_index:0",
                        score=1.0,
                    )
                )
            else:
                generated = generator.execute(planned_task.anomaly_subtype, context)
                candidates = []
                if parameters.get("sql"):
                    candidates.append(str(parameters["sql"]))
                target_table = parameters.get("target_table")
                if target_table:
                    candidates.append(f"SELECT COUNT(*) FROM {target_table}")
                candidates.extend(generated)
                react_trace.append(
                    ReActStep(
                        thought="Generate multiple SQL candidates and prioritize reflexion-provided SQL or target table.",
                        action="generate_slow_sql_candidates",
                        observation={"candidate_count": len(candidates), "target_table": target_table},
                        decision="Validate candidates with static checks and EXPLAIN before selecting.",
                    )
                )
                selected_sql = ""
                for candidate in candidates[:6]:
                    validation = validator.execute(
                        sql=candidate,
                        allowed_tables=allowed_tables
                        + [
                            "agent_order_by_support",
                            "agent_group_by_support",
                            "agent_large_scan_support",
                            "agent_implicit_conversion_support",
                            "agent_excessive_index",
                        ],
                        anomaly_type=planned_task.anomaly_subtype,
                    )
                    if not validation["valid"]:
                        react_trace.append(
                            ReActStep(
                                thought="Candidate SQL must pass static safety and table allow-list checks.",
                                action="dry_run_validate_sql",
                                observation={"sql": candidate, "validation": validation},
                                decision="Reject candidate and continue to the next option.",
                                candidate_id=f"slow_sql:{planned_task.anomaly_subtype}",
                                score=0.0,
                            )
                        )
                        continue
                    explain = explainer.execute(validation["sql"], database=planned_task.database)
                    react_trace.append(
                        ReActStep(
                            thought="Candidate SQL should have an EXPLAIN plan consistent with slow-query pressure.",
                            action="explain_probe",
                            observation={"sql": validation["sql"], "explain": explain},
                            decision="Accept candidate if EXPLAIN validates; otherwise revise to the next candidate.",
                            candidate_id=f"slow_sql:{planned_task.anomaly_subtype}",
                            score=1.0 if explain.get("validated") else 0.2,
                        )
                    )
                    if explain.get("validated"):
                        selected_sql = validation["sql"]
                        validation_steps.insert(0, {"kind": "explain", "sql": validation["sql"], "result": explain})
                        break
            if not selected_sql:
                continue
            multi_mode = request.mode != "single" or request.test_enabled
            default_threads = 8 if any(term in feedback for term in ("not enough", "weak", "qps", "latency", "slow sql")) else 6
            default_sleep = 0.005 if any(term in feedback for term in ("not enough", "weak", "qps", "latency", "slow sql")) else 0.01
            default_duration = 20 if any(term in feedback for term in ("too short", "overlap", "weak", "qps")) else 12
            if multi_mode:
                tuning = {
                    "kind": "workload_profile",
                    "mode": "single_sql",
                    "sleep_time": self._clamp_float(parameters.get("background_sleep"), default_sleep, 0.001, 1.0),
                    "thread_count": self._clamp_int(parameters.get("background_threads"), default_threads, 1, 128),
                    "repeat": self._clamp_int(parameters.get("repeat"), 30 if default_threads > 6 else 25, 1, 500),
                    "description": f"Background slow SQL pressure for {planned_task.anomaly_subtype}.",
                    "sql": selected_sql,
                    "database": planned_task.database,
                    "duration_seconds": self._clamp_int(parameters.get("background_duration_seconds", parameters.get("duration_seconds")), default_duration, 1, 60),
                }
                execution_steps = list(setup_steps) + [tuning]
                task_role = "background_anomaly"
            else:
                execution_steps = list(setup_steps)
                execution_steps.append(
                    {
                        "kind": "sql",
                        "sql": selected_sql,
                        "database": planned_task.database,
                        "hold_seconds": self._clamp_int(parameters.get("hold_seconds"), min(max(request.execution_window_seconds, 1), 12), 1, 60),
                    }
                )
                task_role = "one_shot_sql"
            task_spec = TaskSpec(
                task_id=f"slow-sql-{slugify(planned_task.anomaly_subtype)}",
                agent_type=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                title=f"Slow SQL: {planned_task.anomaly_subtype}",
                task_role=task_role,
                inputs={
                    "database": planned_task.database,
                    "anomaly_subtype": planned_task.anomaly_subtype,
                    "source_agent": self.agent_type,
                    "sql": selected_sql,
                    **parameters,
                },
                prechecks=[],
                execution_steps=execution_steps,
                validation_steps=validation_steps,
                rollback_steps=rollback_steps,
                explanation=planned_task.rationale or f"Generate and run a {planned_task.anomaly_subtype} SQL candidate.",
                expected_metrics={"query_latency": "increase", "rows_examined": "increase", "slow_query_count": "increase"},
                local_success_criteria={"explain_validated": True, "latency_direction": "increase"},
                risk_assessment={"risk_level": "medium", "main_risk": "long-running query pressure", "confidence": 0.7},
                cleanup_actions=rollback_steps,
            )
            outputs.append(
                TaskAgentOutput(
                    agent_name=self.agent_type,
                    subgoal=task_input.subgoal,
                    local_hypothesis=f"{planned_task.anomaly_subtype} SQL should increase scan work or latency.",
                    task_spec=task_spec,
                    expected_metrics=task_spec.expected_metrics,
                    local_success_criteria=task_spec.local_success_criteria,
                    risk_assessment=task_spec.risk_assessment,
                    safety_constraints=task_input.constraints,
                    cleanup_actions=task_spec.cleanup_actions,
                    fallback_plan={"template_sql_available": True},
                    confidence=0.7,
                    react_trace=react_trace
                    + [
                        ReActStep(
                            thought="Select the highest-confidence EXPLAIN-validated slow SQL candidate.",
                            action="select_final_task_spec",
                            observation={"sql": selected_sql, "parameters": parameters},
                            decision="Finalize slow SQL TaskSpec with reflexion-adjusted pressure settings.",
                            candidate_id=f"slow_sql:{planned_task.anomaly_subtype}:final",
                            score=0.7,
                            adjustments={key: parameters[key] for key in parameters if key in {"background_threads", "background_sleep", "background_duration_seconds", "target_table", "sql"}},
                        )
                    ],
                )
            )
        return outputs

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
