"""
Safety checker: validates an ExecutableTaskDAG against resource limits
and dangerous-pattern rules before experiment execution.
"""

from __future__ import annotations

import re
from typing import Any

from agent.config import RuntimeConfig
from agent.types import SafetyResult
from agent.tools import validate_traffic_surge_profile


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

EXECUTOR_GRACE_SEC = 30.0
DEFAULT_LOGICAL_BACKUP_DURATION_SEC = 120.0


class SafetyChecker:
    """Pre-execution safety gate for task DAGs."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def check(
        self,
        task_dag: dict,
        current_db_metrics: dict | None = None,
        current_os_metrics: dict | None = None,
        max_duration_sec: float | None = None,
        injection_observe_sec: float | None = None,
        expected_workload: dict[str, Any] | None = None,
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
        duration_limit = float(max_duration_sec if max_duration_sec is not None else self.config.max_duration_sec)
        required_sec, timing_reasons = estimate_dag_required_sec(
            task_dag,
            include_grace=False,
            reject_workload_ramp=bool(expected_workload),
        )
        reasons.extend(timing_reasons)
        if required_sec > duration_limit:
            reasons.append(
                f"DAG required duration {required_sec}s exceeds limit "
                f"{duration_limit}s"
            )
        if injection_observe_sec is not None:
            required_with_offsets, injection_reasons = estimate_dag_required_sec(
                task_dag,
                include_grace=False,
                reject_workload_ramp=bool(expected_workload),
            )
            reasons.extend(injection_reasons)
            if required_with_offsets > float(injection_observe_sec):
                reasons.append(
                    f"DAG required duration {required_with_offsets}s exceeds "
                    f"injection_observe_sec {float(injection_observe_sec)}s"
                )

        # -------------------------------------------------------------------------
        # Connection usage check
        # -------------------------------------------------------------------------
        if current_db_metrics:
            max_conn = int(current_db_metrics.get("max_connections", 100))
            current_conn = int(
                current_db_metrics.get("Threads_connected", 0)
            )
            estimated_additional = sum(_action_connection_estimate(a) for task in tasks.values() for a in task.get("actions", []))
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
                elif action.get("kind") == "benchbase_burst":
                    try:
                        profile = validate_traffic_surge_profile(action.get("profile") or {})
                        if profile.get("rate") == "unlimited":
                            warnings.append(f"Task '{task_id}' BenchBase burst uses unlimited rate")
                        if expected_workload:
                            expected_benchmark = str(expected_workload.get("benchmark") or "").lower()
                            if expected_benchmark and profile["benchmark"] != expected_benchmark:
                                reasons.append(
                                    f"Task '{task_id}' benchmark {profile['benchmark']} does not match "
                                    f"background workload {expected_benchmark}"
                                )
                            expected_database = str(expected_workload.get("database") or "")
                            if expected_database and profile["database"] != expected_database:
                                reasons.append(
                                    f"Task '{task_id}' database {profile['database']} does not match "
                                    f"background workload {expected_database}"
                                )
                            expected_config_path = str(expected_workload.get("config_path") or "")
                            if expected_config_path and profile["config_path"] != expected_config_path:
                                reasons.append(f"Task '{task_id}' config_path does not match background workload config_path")
                    except Exception as exc:
                        reasons.append(f"Task '{task_id}' has invalid TrafficSurgeProfile: {exc}")
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
                    if dur > duration_limit:
                        reasons.append(
                            f"ChaosBlade duration {dur}s in task '{task_id}' "
                            f"exceeds max {duration_limit}s"
                        )

        # -------------------------------------------------------------------------
        # Overall decision
        # -------------------------------------------------------------------------
        approved = len(reasons) == 0
        return SafetyResult(approved=approved, reasons=reasons, warnings=warnings)


def action_duration_sec(action: dict[str, Any]) -> float:
    if action.get("kind") == "benchbase_burst":
        profile = action.get("profile") or {}
        return float(profile.get("duration_sec", action.get("duration_sec", 0)) or 0)
    if action.get("kind") == "lock_conflict":
        return float(action.get("hold_sec", action.get("duration_sec", 0)) or 0)
    if action.get("kind") == "logical_backup":
        return float(action.get("duration_sec", DEFAULT_LOGICAL_BACKUP_DURATION_SEC) or DEFAULT_LOGICAL_BACKUP_DURATION_SEC)
    return float(action.get("duration_sec", 0) or 0)


def _action_connection_estimate(action: dict[str, Any]) -> int:
    kind = action.get("kind")
    if kind == "benchbase_burst":
        profile = action.get("profile") or {}
        return int(profile.get("terminals", action.get("terminals", 0)) or 0)
    if kind == "workload_ramp":
        stages = action.get("ramp_stages") or []
        if isinstance(stages, list):
            return max((int((stage or {}).get("connections", 0) or 0) for stage in stages if isinstance(stage, dict)), default=0)
        return 0
    return int(action.get("concurrency", 0) or 0)


def action_grace_sec(action: dict[str, Any]) -> float:
    if action.get("kind") in {"benchbase_burst", "sql_workload", "chaosblade", "lock_conflict", "logical_backup"}:
        return EXECUTOR_GRACE_SEC
    return 0.0


def estimate_dag_required_sec(
    task_dag: dict[str, Any],
    *,
    include_grace: bool = False,
    reject_workload_ramp: bool = False,
) -> tuple[float, list[str]]:
    tasks = task_dag.get("tasks", {}) or {}
    schedule = task_dag.get("schedule", {}) or {}
    reasons: list[str] = []
    max_end = 0.0
    for task_id, task in tasks.items():
        task_offset = float((schedule or {}).get(task_id, 0) or 0) + float(task.get("start_after_sec", 0) or 0)
        elapsed = task_offset
        for idx, action in enumerate(task.get("actions", []) or []):
            kind = action.get("kind", "")
            if reject_workload_ramp and kind == "workload_ramp":
                reasons.append(f"Task '{task_id}' action {idx} uses legacy workload_ramp, which is not allowed with workload enabled")
            duration = action_duration_sec(action)
            if duration <= 0:
                reasons.append(f"Task '{task_id}' action {idx} kind={kind or '<missing>'} has non-positive duration")
            elapsed += max(0.0, duration)
            if include_grace:
                elapsed += action_grace_sec(action)
        max_end = max(max_end, elapsed)
    return max_end, reasons
