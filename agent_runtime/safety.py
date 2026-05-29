from __future__ import annotations

import os
import re

from agent_runtime.types import EnvironmentSnapshot, ExperimentRequest, SafetyCheckResult, TaskDAG


class SafetyChecker:
    DANGEROUS_SQL = re.compile(r"\b(drop\s+database|truncate\s+table|delete\s+from\s+\w+\s*(;|$))", re.IGNORECASE)
    DANGEROUS_COMMANDS = ("rm -rf", "mkfs", "shutdown", "reboot", "disk erase")

    def check(self, task_dag: TaskDAG, env: EnvironmentSnapshot, request: ExperimentRequest) -> SafetyCheckResult:
        if os.environ.get("DBMAGS_DISABLE_SAFETY", "").strip().lower() in {"1", "true", "yes", "on"}:
            return SafetyCheckResult(
                approved=True,
                reasons=[],
                warnings=["safety checker disabled via DBMAGS_DISABLE_SAFETY"],
                checked_constraints={"disabled": True},
            )
        constraints = {
            "max_duration_sec": request.safety_constraints.get("max_duration_sec", request.max_retry_rounds * max(request.execution_window_seconds, 1)),
            "max_connection_usage_ratio": request.safety_constraints.get("max_connection_usage_ratio", 0.8),
            "max_cpu_usage": request.safety_constraints.get("max_cpu_usage", 95),
        }
        reasons: list[str] = []
        warnings: list[str] = []
        total_duration = sum(_task_duration(node.task_spec) for node in task_dag.tasks.values())
        if total_duration > float(constraints["max_duration_sec"]):
            reasons.append(f"estimated duration {total_duration:.1f}s exceeds max_duration_sec={constraints['max_duration_sec']}")

        max_connections = float(env.db_metrics.get("max_connections", 0) or 0)
        estimated_connections = sum(_task_threads(node.task_spec) for node in task_dag.tasks.values())
        if max_connections and estimated_connections > max_connections * float(constraints["max_connection_usage_ratio"]):
            reasons.append("estimated connection usage exceeds configured safe ratio")

        cpu_percent = float(env.os_metrics.get("cpu_percent", 0) or 0)
        if cpu_percent > float(constraints["max_cpu_usage"]):
            reasons.append(f"current CPU usage {cpu_percent:.1f}% exceeds max_cpu_usage")

        for node in task_dag.tasks.values():
            task = node.task_spec
            if not task.rollback_steps and task.task_role in {"backup_job", "lock_holder"}:
                warnings.append(f"{task.task_id} has no explicit rollback steps")
            for step in task.execution_steps:
                sql = str(step.get("sql", ""))
                command = str(step.get("command", ""))
                if sql and self.DANGEROUS_SQL.search(sql):
                    reasons.append(f"{task.task_id} contains dangerous SQL")
                if command and any(item in command.lower() for item in self.DANGEROUS_COMMANDS):
                    reasons.append(f"{task.task_id} contains dangerous command")

        return SafetyCheckResult(
            approved=not reasons,
            reasons=reasons,
            warnings=warnings,
            checked_constraints=constraints,
        )


def _task_duration(task) -> float:
    durations = []
    for step in task.execution_steps:
        durations.append(float(step.get("duration_seconds") or step.get("hold_seconds") or task.inputs.get("duration_seconds") or 0))
    return max(durations or [0.0])


def _task_threads(task) -> int:
    """Estimate concurrent DB sessions for this task.

    workload_profile stores thread_count as load-tuning metadata, but
    RunInjectionSkill runs a single client loop (one connection at a time).
    Count one session per workload step; other steps default to one session.
    """
    count = 0
    for step in task.execution_steps:
        if step.get("kind") == "workload_profile":
            count += 1
    return count or 1
