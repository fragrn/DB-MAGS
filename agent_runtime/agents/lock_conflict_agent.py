from __future__ import annotations

from uuid import uuid4
from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec
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
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ) -> List[TaskAgentOutput]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        preparer = self.skills.get("prepare_lock_sql_skill")
        generator = self.skills.get("generate_sql_candidate_skill")
        validator = self.skills.get("validate_sql_skill")
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
            column_name = planned_task.parameters.get("column_name", f"agent_meta_lock_{uuid4().hex[:8]}")
            default_duration = min(max(request.execution_window_seconds, 1), 12)
            if any(term in feedback for term in ("too short", "holder", "waiter", "non-hot", "weak", "qps")):
                default_duration = max(default_duration, 20)
            duration_seconds = self._clamp_int(
                parameters.get("hold_seconds", parameters.get("background_duration_seconds")),
                default_duration,
                1,
                60,
            )
            payload = preparer.execute(
                anomaly_subtype=planned_task.anomaly_subtype,
                database=planned_task.database,
                duration_seconds=duration_seconds,
                column_name=str(column_name),
                multi_mode=request.mode != "single",
            )
            target_table = str(parameters.get("target_table", "new_orders"))
            predicate = str(parameters.get("predicate", "no_w_id = 1 AND no_d_id = 1 AND no_o_id > 10"))
            candidates = generator.execute_structured(
                agent_name=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                subgoal=task_input.subgoal,
                db_context=context,
                task_input=task_input,
                constraints={"sql_constraints": ["Lock SQL must be suitable for a lock holder or waiter and must not be irreversible."]},
                candidate_count=4,
            )
            selected_sql = ""
            react_trace.append(
                ReActStep(
                    thought="Generate lock SQL candidates with the lock-specific LLM prompt.",
                    action="generate_sql_candidates_with_llm",
                    observation={
                        "candidate_count": len(candidates),
                        "candidates": candidates,
                        "used_static_fallback": any(item.get("source") == "static_fallback" for item in candidates if isinstance(item, dict)),
                    },
                    decision="Validate candidate lock SQL before replacing the default holder SQL.",
                )
            )
            for candidate in candidates:
                sql = candidate.get("sql", "") if isinstance(candidate, dict) else str(candidate)
                validation = validator.execute(sql=sql, allowed_tables=allowed_tables, anomaly_type=planned_task.anomaly_subtype)
                lock_valid = validation.get("valid") and self._lock_sql_matches(planned_task.anomaly_subtype, validation.get("sql", ""))
                react_trace.append(
                    ReActStep(
                        thought="Lock candidates must match the requested lock anomaly and pass SQL safety checks.",
                        action="validate_sql_safety",
                        observation={"candidate": candidate, "validation": validation, "lock_valid": lock_valid},
                        decision="Use this candidate if it can serve as the final lock holder SQL.",
                        candidate_id=f"lock:{planned_task.anomaly_subtype}:sql",
                        score=0.8 if lock_valid else 0.0,
                    )
                )
                if lock_valid:
                    selected_sql = str(validation["sql"])
                    break
            payload = self._apply_reflexion_lock_sql(
                payload,
                planned_task.anomaly_subtype,
                target_table,
                predicate,
                duration_seconds,
                selected_sql=selected_sql,
            )
            react_trace.extend(
                [
                    ReActStep(
                        thought="Generate holder/waiter lock candidate from planner parameters and reflexion.",
                        action="generate_lock_candidates",
                        observation={"target_table": target_table, "predicate": predicate, "hold_seconds": duration_seconds},
                        decision="Use longer holder duration or provided target predicate when reflexion says contention was weak.",
                        candidate_id=f"lock:{planned_task.anomaly_subtype}",
                        adjustments={"target_table": target_table, "predicate": predicate, "hold_seconds": duration_seconds},
                    ),
                    ReActStep(
                        thought="Validate lock candidate with schema-level checks before finalizing.",
                        action="schema_probe",
                        observation={"available_tables": [table.name for table in context.tables], "target_table": target_table},
                        decision="Finalize lock TaskSpec if target table is present or fallback lock SQL remains available.",
                        candidate_id=f"lock:{planned_task.anomaly_subtype}",
                        score=0.75 if any(table.name == target_table for table in context.tables) else 0.45,
                    ),
                ]
            )
            task_spec = TaskSpec(
                task_id=f"lock-{slugify(planned_task.anomaly_subtype)}",
                agent_type=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                title=payload["title"],
                task_role="lock_holder",
                inputs={
                    "database": planned_task.database,
                    "anomaly_subtype": planned_task.anomaly_subtype,
                    "source_agent": self.agent_type,
                    "column_name": column_name,
                    "target_table": target_table,
                    "predicate": predicate,
                    "hold_seconds": duration_seconds,
                    **parameters,
                },
                prechecks=[],
                execution_steps=payload["execution_steps"],
                validation_steps=[],
                rollback_steps=payload["rollback_steps"],
                explanation=planned_task.rationale or payload["title"],
                expected_metrics={"lock_waits": "increase", "blocked_transactions": "increase", "p95_latency": "increase"},
                local_success_criteria={"lock_wait_time_increase_ratio": 2.0, "blocked_transaction_min_count": 1},
                risk_assessment={"risk_level": "medium", "main_risk": "deadlock or timeout", "confidence": 0.75},
                cleanup_actions=payload["rollback_steps"],
            )
            outputs.append(
                TaskAgentOutput(
                    agent_name=self.agent_type,
                    subgoal=task_input.subgoal,
                    local_hypothesis=f"{planned_task.anomaly_subtype} should introduce waiting during probe execution.",
                    task_spec=task_spec,
                    expected_metrics=task_spec.expected_metrics,
                    local_success_criteria=task_spec.local_success_criteria,
                    risk_assessment=task_spec.risk_assessment,
                    safety_constraints=task_input.constraints,
                    cleanup_actions=task_spec.cleanup_actions,
                    fallback_plan={"lock_subtype": planned_task.anomaly_subtype},
                    confidence=0.75,
                    react_trace=react_trace,
                )
            )
        return outputs

    @staticmethod
    def _apply_reflexion_lock_sql(payload: dict, subtype: str, target_table: str, predicate: str, hold_seconds: int, selected_sql: str = "") -> dict:
        if target_table == "new_orders" and not predicate:
            return payload
        execution_steps = []
        for step in payload.get("execution_steps", []):
            updated = dict(step)
            if selected_sql:
                updated["kind"] = "hold_sql" if not selected_sql.lower().startswith("alter") else "sql"
                updated["sql"] = selected_sql
            elif subtype == "record_lock" and target_table:
                updated["kind"] = "hold_sql"
                updated["sql"] = f"SELECT * FROM {target_table} WHERE {predicate} FOR UPDATE"
            elif subtype == "table_lock" and target_table:
                updated["kind"] = "hold_sql"
                updated["sql"] = f"LOCK TABLES {target_table} WRITE"
            elif subtype == "metadata_lock" and target_table:
                updated["kind"] = "hold_metadata_lock"
                updated["sql"] = f"SELECT COUNT(*) FROM {target_table}"
            updated["hold_seconds"] = hold_seconds
            execution_steps.append(updated)
        payload = dict(payload)
        payload["execution_steps"] = execution_steps
        return payload

    @staticmethod
    def _lock_sql_matches(subtype: str, sql: object) -> bool:
        lowered = str(sql).strip().lower()
        if subtype == "record_lock":
            return (" for update" in lowered and lowered.startswith("select")) or (lowered.startswith("update") and " where " in lowered)
        if subtype == "table_lock":
            return lowered.startswith("lock tables")
        if subtype == "metadata_lock":
            return lowered.startswith("select") or lowered.startswith("alter")
        return False

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
