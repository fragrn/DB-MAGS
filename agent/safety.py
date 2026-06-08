"""
Safety checker: validates an ExecutableTaskDAG against resource limits
and dangerous-pattern rules before experiment execution.
"""

from __future__ import annotations

import re
from typing import Any

from agent.config import RuntimeConfig
from agent.types import SafetyResult


# ---------------------------------------------------------------------------
# Dangerous pattern definitions
# ---------------------------------------------------------------------------

_DANGEROUS_SQL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bGRANT\b", re.IGNORECASE),
    re.compile(r"\bREVOKE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+\w+\s*$", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+\w+\s*SET\s*\w+\s*=\s*\w+\s*$", re.IGNORECASE),
]

_DANGEROUS_CMD_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
]

_PROD_DB_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bprod\b", re.IGNORECASE),
    re.compile(r"\bproduction\b", re.IGNORECASE),
    re.compile(r"\bonline\b", re.IGNORECASE),
]


class SafetyChecker:
    """Pre-execution safety gate for task DAGs."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def check(
        self,
        task_dag: dict,
        current_db_metrics: dict | None = None,
        current_os_metrics: dict | None = None,
    ) -> SafetyResult:
        """
        Evaluate an ExecutableTaskDAG dict and return a SafetyResult.
        """
        reasons: list[str] = []
        warnings: list[str] = []

        tasks = task_dag.get("tasks", {})
        if not tasks:
            reasons.append("DAG has no tasks")
            return SafetyResult(approved=False, reasons=reasons, warnings=warnings)

        # -------------------------------------------------------------------------
        # Duration check
        # -------------------------------------------------------------------------
        total_duration = sum(
            a.get("duration_sec", 0)
            for task in tasks.values()
            for a in task.get("actions", [])
        )
        if total_duration > self.config.max_duration_sec:
            reasons.append(
                f"Total task duration {total_duration}s exceeds limit "
                f"{self.config.max_duration_sec}s"
            )

        # -------------------------------------------------------------------------
        # Connection usage check
        # -------------------------------------------------------------------------
        if current_db_metrics:
            max_conn = int(current_db_metrics.get("max_connections", 100))
            current_conn = int(
                current_db_metrics.get("Threads_connected", 0)
            )
            estimated_additional = sum(
                a.get("concurrency", 0)
                for task in tasks.values()
                for a in task.get("actions", [])
            )
            total_needed = current_conn + estimated_additional
            limit = int(max_conn * self.config.max_connection_usage_ratio)
            if total_needed > limit:
                reasons.append(
                    f"Connection usage {total_needed}/{max_conn} exceeds "
                    f"{self.config.max_connection_usage_ratio * 100:.0f}% limit"
                )

        # -------------------------------------------------------------------------
        # CPU / memory check
        # -------------------------------------------------------------------------
        if current_os_metrics:
            cpu = current_os_metrics.get("cpu_usage", {}).get("usage_ratio", 0.0)
            if cpu > self.config.max_cpu_usage / 100.0:
                reasons.append(
                    f"Current CPU usage {cpu * 100:.1f}% already above "
                    f"{self.config.max_cpu_usage}%"
                )

        # -------------------------------------------------------------------------
        # Production DB name check
        # -------------------------------------------------------------------------
        db_name = self.config.default_database
        if any(p.search(db_name) for p in _PROD_DB_PATTERNS):
            reasons.append(
                f"Database name '{db_name}' looks like production; "
                "use a dedicated test database"
            )

        # -------------------------------------------------------------------------
        # Dangerous SQL / command check
        # -------------------------------------------------------------------------
        for task_id, task in tasks.items():
            for action in task.get("actions", []):
                if action.get("kind") == "sql_workload":
                    sql = action.get("sql", "")
                    for pat in _DANGEROUS_SQL_PATTERNS:
                        if pat.search(sql):
                            reasons.append(
                                f"Task '{task_id}' contains dangerous SQL pattern: "
                                f"{pat.pattern}"
                            )
                elif action.get("kind") == "logical_backup":
                    tool = action.get("tool", "")
                    if not tool:
                        warnings.append(f"Task '{task_id}' backup has no tool specified")
                cmd = action.get("command", "")
                for pat in _DANGEROUS_CMD_PATTERNS:
                    if pat.search(cmd):
                        reasons.append(
                            f"Task '{task_id}' contains dangerous command: {pat.pattern}"
                        )

        # -------------------------------------------------------------------------
        # Lock/task risk check
        # -------------------------------------------------------------------------
        for task_id, task in tasks.items():
            if task.get("task_type") in ("lock_conflict",):
                if not task.get("cleanup_actions"):
                    warnings.append(
                        f"Task '{task_id}' of type lock_conflict has no cleanup_actions"
                    )

        # -------------------------------------------------------------------------
        # ChaosBlade parameter limits
        # -------------------------------------------------------------------------
        for task_id, task in tasks.items():
            for action in task.get("actions", []):
                if action.get("kind") == "chaosblade":
                    dur = action.get("duration_sec", 0)
                    if dur > self.config.max_duration_sec:
                        reasons.append(
                            f"ChaosBlade duration {dur}s in task '{task_id}' "
                            f"exceeds max {self.config.max_duration_sec}s"
                        )

        # -------------------------------------------------------------------------
        # Overall decision
        # -------------------------------------------------------------------------
        approved = len(reasons) == 0
        return SafetyResult(approved=approved, reasons=reasons, warnings=warnings)
