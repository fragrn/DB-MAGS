from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent_runtime.types import DBContextSummary, DBTableProfile, ExperimentRequest, PlannedAnomaly, ReActStep, TaskAgentInput, TaskAgentOutput, TaskSpec


class BaseTaskAgent(ABC):
    agent_type = "base"

    @abstractmethod
    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: list[PlannedAnomaly] | None = None,
        task_inputs: dict[str, TaskAgentInput] | None = None,
    ):
        raise NotImplementedError

    def _input_for(self, subtype: str, task_inputs: dict[str, TaskAgentInput] | None) -> TaskAgentInput:
        return (task_inputs or {}).get(subtype, TaskAgentInput(subgoal=subtype))

    def _reflection(self, task_input: TaskAgentInput) -> dict[str, Any]:
        memory = task_input.memory if isinstance(task_input.memory, dict) else {}
        reflection = memory.get("latest_reflection", {})
        return reflection if isinstance(reflection, dict) else {}

    def _reflection_updates(self, task_input: TaskAgentInput, subtype: str) -> dict[str, Any]:
        reflection = self._reflection(task_input)
        updates = reflection.get("task_parameter_updates", {})
        if not isinstance(updates, dict):
            return {}
        subtype_updates = updates.get(subtype, {})
        return subtype_updates if isinstance(subtype_updates, dict) else {}

    def _feedback_text(self, task_input: TaskAgentInput) -> str:
        reflection = self._reflection(task_input)
        memory = task_input.memory if isinstance(task_input.memory, dict) else {}
        pieces: list[str] = []
        for key in ("failure_reason", "suggested_changes", "risk_warning", "memory_update"):
            value = reflection.get(key)
            if isinstance(value, list):
                pieces.extend(str(item) for item in value)
            elif value:
                pieces.append(str(value))
        feedback = reflection.get("agent_specific_feedback", {})
        if isinstance(feedback, dict):
            for key in (self.agent_type, task_input.subgoal):
                value = feedback.get(key)
                if isinstance(value, list):
                    pieces.extend(str(item) for item in value)
                elif value:
                    pieces.append(str(value))
        for item in memory.get("long_term_memory", []) if isinstance(memory, dict) else []:
            if isinstance(item, dict):
                lesson = item.get("lesson") or item.get("context")
                if lesson:
                    pieces.append(str(lesson))
        return " ".join(pieces).lower()

    def _merge_parameters(self, planned_task: PlannedAnomaly, task_input: TaskAgentInput) -> dict[str, Any]:
        parameters = dict(planned_task.parameters)
        updates = self._reflection_updates(task_input, planned_task.anomaly_subtype)
        if updates:
            parameters.update(updates)
        return parameters

    def _memory_trace(self, task_input: TaskAgentInput, subtype: str, parameters: dict[str, Any]) -> ReActStep:
        reflection = self._reflection(task_input)
        return ReActStep(
            thought="Need to incorporate reflexion and memory before generating task candidates.",
            action="read_reflection_memory",
            observation={
                "latest_reflection": reflection,
                "short_term_rounds": len(task_input.memory.get("short_term_trace", [])) if isinstance(task_input.memory, dict) else 0,
                "long_term_items": len(task_input.memory.get("long_term_memory", [])) if isinstance(task_input.memory, dict) else 0,
                "effective_parameters": parameters,
            },
            decision=f"Use reflexion and memory as planning context for {subtype} alongside the GlobalPlanner subgoal.",
            adjustments=self._reflection_updates(task_input, subtype),
        )

    @staticmethod
    def _metric_sample_trace(task_input: TaskAgentInput, subtype: str) -> ReActStep:
        memory = task_input.memory if isinstance(task_input.memory, dict) else {}
        short_term = memory.get("short_term_trace", [])
        latest = short_term[-1] if isinstance(short_term, list) and short_term else {}
        if not isinstance(latest, dict):
            latest = {}
        return ReActStep(
            thought="Use the latest episode metrics as a low-risk planning-time sample.",
            action="metric_sample_probe",
            observation={
                "baseline_metrics": latest.get("baseline_metrics", {}),
                "after_metrics": latest.get("after_metrics", {}),
                "evaluation": latest.get("evaluation", {}),
            },
            decision=f"Adjust {subtype} candidates when prior metrics show weak degradation.",
            candidate_id=f"{subtype}:metric-sample",
        )

    @staticmethod
    def _table_stats(context: DBContextSummary) -> dict[str, int]:
        return {table.name: int(table.row_count or 0) for table in context.tables}

    @staticmethod
    def _largest_tables(context: DBContextSummary, include_agent_tables: bool = False) -> list[DBTableProfile]:
        candidates = [
            table
            for table in context.tables
            if include_agent_tables or (not table.name.startswith("agent_") and not table.name.endswith("_backup_agent"))
        ]
        return sorted(candidates, key=lambda table: table.row_count or 0, reverse=True)

    @staticmethod
    def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = default
        return min(max(number, low), high)

    @staticmethod
    def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return min(max(number, low), high)

    @staticmethod
    def _output_for(task_spec: TaskSpec, task_input: TaskAgentInput, hypothesis: str, fallback_plan: dict | None = None) -> TaskAgentOutput:
        return TaskAgentOutput(
            agent_name=task_spec.agent_type,
            subgoal=task_input.subgoal,
            local_hypothesis=hypothesis,
            task_spec=task_spec,
            expected_metrics=task_spec.expected_metrics,
            local_success_criteria=task_spec.local_success_criteria,
            risk_assessment=task_spec.risk_assessment,
            safety_constraints=task_input.constraints,
            cleanup_actions=task_spec.cleanup_actions or task_spec.rollback_steps,
            fallback_plan=fallback_plan or {},
            confidence=float(task_spec.risk_assessment.get("confidence", 0.6)) if task_spec.risk_assessment else 0.6,
            react_trace=list(task_input.memory.get("react_trace", [])),
        )

    @abstractmethod
    def explain(self, task_spec: TaskSpec) -> str:
        raise NotImplementedError
