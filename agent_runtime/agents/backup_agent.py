from __future__ import annotations

import re
from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec
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
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ) -> List[TaskAgentOutput]:
        planned = [task for task in (planner_tasks or []) if task.anomaly_subtype in self.supported_subtypes]
        preparer = self.skills.get("prepare_backup_task_skill")
        generator = self.skills.get("generate_sql_candidate_skill")
        outputs: List[TaskAgentOutput] = []
        for planned_task in planned:
            task_input = self._input_for(planned_task.anomaly_subtype, task_inputs)
            parameters = self._merge_parameters(planned_task, task_input)
            candidates = generator.execute_structured(
                agent_name=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                subgoal=task_input.subgoal,
                db_context=context,
                task_input=task_input,
                constraints={"sql_constraints": ["Suggest source_table and backup_table for a rollback-safe backup interference task."]},
                candidate_count=4,
            )
            source_table = str(parameters.get("source_table") or self._source_table_from_candidates(candidates))
            if not source_table:
                react_trace = self._react_trace(context, task_input, parameters, planned_task.anomaly_subtype, candidates, "")
                react_trace.append(
                    ReActStep(
                        thought="No LLM backup candidate produced a usable source table.",
                        action="revise_candidate_if_needed",
                        observation={"candidate_count": len(candidates)},
                        decision="Skip this backup task because no hard-coded source table fallback is allowed.",
                        score=0.0,
                    )
                )
                outputs.append(
                    TaskAgentOutput(
                        agent_name=self.agent_type,
                        subgoal=task_input.subgoal,
                        local_hypothesis="No backup SQL task was generated because LLM did not provide a safe source table.",
                        task_spec=TaskSpec(
                            task_id="backup-no-candidate",
                            agent_type=self.agent_type,
                            anomaly_type=planned_task.anomaly_subtype,
                            title="No backup candidate generated",
                            task_role="planning_failed",
                            inputs={"database": planned_task.database, "anomaly_subtype": planned_task.anomaly_subtype},
                            explanation="LLM did not return a usable backup source table.",
                        ),
                        fallback_plan={},
                        confidence=0.0,
                        react_trace=react_trace,
                    )
                )
                continue
            backup_table = str(parameters.get("backup_table") or self._backup_table_from_candidates(candidates) or f"{source_table}_backup_agent")
            react_trace = self._react_trace(context, task_input, parameters, planned_task.anomaly_subtype, candidates, source_table)
            payload = preparer.execute(database=planned_task.database, source_table=source_table, backup_table=backup_table)
            feedback = self._feedback_text(task_input)
            default_duration = request.execution_window_seconds
            if any(term in feedback for term in ("too short", "overlap", "not enough", "weak", "qps")):
                default_duration = max(default_duration, 20)
            duration = self._clamp_int(parameters.get("background_duration_seconds", parameters.get("duration_seconds")), default_duration, 1, 60)
            task_spec = TaskSpec(
                task_id=f"backup-{slugify(source_table)}",
                agent_type=self.agent_type,
                anomaly_type=planned_task.anomaly_subtype,
                title=payload["title"],
                task_role="backup_job",
                inputs={
                    "database": planned_task.database,
                    "anomaly_subtype": planned_task.anomaly_subtype,
                    "source_agent": self.agent_type,
                    "source_table": source_table,
                    "backup_table": backup_table,
                    "background_duration_seconds": duration,
                    "concurrent_with_probe": bool(parameters.get("concurrent_with_probe", True if feedback else False)),
                    **parameters,
                },
                prechecks=[],
                execution_steps=payload["execution_steps"],
                validation_steps=[{"kind": "react_trace", "result": {"steps": [step.__dict__ for step in react_trace]}}],
                rollback_steps=payload["rollback_steps"],
                explanation=planned_task.rationale or payload["title"],
                expected_metrics={"disk_read_throughput": "increase", "io_wait": "increase", "query_latency": "increase"},
                local_success_criteria={"backup_table_created": True, "source_table": source_table},
                risk_assessment={"risk_level": "medium", "main_risk": "IO interference or metadata lock", "confidence": 0.72},
                cleanup_actions=payload["rollback_steps"],
            )
            output = TaskAgentOutput(
                agent_name=self.agent_type,
                subgoal=task_input.subgoal,
                local_hypothesis=f"Backing up {source_table} should create stronger overlap with TPCC probe pressure.",
                task_spec=task_spec,
                expected_metrics=task_spec.expected_metrics,
                local_success_criteria=task_spec.local_success_criteria,
                risk_assessment=task_spec.risk_assessment,
                safety_constraints=task_input.constraints,
                cleanup_actions=task_spec.cleanup_actions,
                fallback_plan={"source_table": "orders", "reason": "default TPCC table if memory-guided selection is unavailable"},
                confidence=0.72,
                react_trace=react_trace,
            )
            outputs.append(output)
        return outputs

    @staticmethod
    def _choose_source_table(context: DBContextSummary, task_input: TaskAgentInput) -> str:
        reflection = task_input.memory.get("latest_reflection", {}) if isinstance(task_input.memory, dict) else {}
        updates = reflection.get("task_parameter_updates", {}) if isinstance(reflection, dict) else {}
        backup_updates = updates.get("database_table_backup", {}) if isinstance(updates, dict) else {}
        if backup_updates.get("source_table"):
            return str(backup_updates["source_table"])
        if reflection:
            candidates = [table for table in context.tables if not table.name.startswith("agent_") and not table.name.endswith("_backup_agent")]
            largest = sorted(candidates, key=lambda table: table.row_count or 0, reverse=True)
            if largest:
                return largest[0].name
        return "orders"

    def _react_trace(
        self,
        context: DBContextSummary,
        task_input: TaskAgentInput,
        parameters: dict,
        subtype: str,
        candidates: list[dict],
        selected: str,
    ) -> list[ReActStep]:
        table_stats = {table.name: table.row_count for table in context.tables}
        latest_reflection = task_input.memory.get("latest_reflection", {}) if isinstance(task_input.memory, dict) else {}
        largest = [table.name for table in self._largest_tables(context)[:3]]
        return [
            self._memory_trace(task_input, subtype, parameters),
            self._metric_sample_trace(task_input, subtype),
            ReActStep(
                thought="Ask the backup-specific LLM prompt to propose diverse source/backup table candidates.",
                action="generate_sql_candidates_with_llm",
                observation={
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                },
                decision="Use LLM only to choose backup strategy; final SQL is regenerated locally for rollback safety.",
            ),
            ReActStep(
                thought="Generate candidate backup targets from reflection and table statistics.",
                action="generate_backup_candidates",
                observation={"reflection": latest_reflection, "largest_tables": largest},
                decision=f"Candidate source table is {selected}.",
                candidate_id=f"backup:{selected}",
                adjustments={"source_table": selected},
            ),
            ReActStep(
                thought="Validate that the backup candidate is reversible and likely to overlap with probe pressure.",
                action="inspect_table_stats",
                observation={"table_row_counts": table_stats},
                decision=f"Select {selected} as backup source table and attach rollback for the backup table.",
                candidate_id=f"backup:{selected}",
                score=float(table_stats.get(str(selected), 0) or 0),
            ),
        ]

    @staticmethod
    def _source_table_from_candidates(candidates: list[dict]) -> str:
        for candidate in candidates:
            metadata = candidate.get("metadata", {}) if isinstance(candidate, dict) else {}
            for key in ("source_table", "table"):
                if isinstance(metadata, dict) and metadata.get(key):
                    return str(metadata[key])
                if isinstance(candidate, dict) and candidate.get(key):
                    return str(candidate[key])
            sql = str(candidate.get("sql", "")) if isinstance(candidate, dict) else ""
            match = re.search(r"\bfrom\s+([a-zA-Z0-9_\.]+)", sql, flags=re.IGNORECASE)
            if match:
                return match.group(1).split(".")[-1]
        return ""

    @staticmethod
    def _backup_table_from_candidates(candidates: list[dict]) -> str:
        for candidate in candidates:
            metadata = candidate.get("metadata", {}) if isinstance(candidate, dict) else {}
            for key in ("backup_table", "target_table"):
                if isinstance(metadata, dict) and metadata.get(key):
                    return str(metadata[key])
                if isinstance(candidate, dict) and candidate.get(key):
                    return str(candidate[key])
            sql = str(candidate.get("sql", "")) if isinstance(candidate, dict) else ""
            match = re.search(r"create\s+table\s+([a-zA-Z0-9_\.]+)", sql, flags=re.IGNORECASE)
            if match:
                return match.group(1).split(".")[-1]
        return ""

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
