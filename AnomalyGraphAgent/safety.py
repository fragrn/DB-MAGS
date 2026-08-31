"""
Safety checker: validates an ExecutableTaskDAG against resource limits
and dangerous-pattern rules before experiment execution.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
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
    re.compile(r"\bDELETE\s+FROM\s+\w+\b(?![\s\S]*\bWHERE\b)", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+\w+\s+SET\b(?![\s\S]*\bWHERE\b)", re.IGNORECASE),
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
RESOURCE_ROOT_CAUSES = {
    "resource_cpu",
    "resource_io",
    "resource_memory",
    "resource_network",
    "network_latency",
    "disk_full_or_pressure",
}
CHAOSBLADE_RESOURCE_TARGETS = {"cpu", "disk", "mem", "network"}


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
            task_actions = list(task.get("actions", []) or []) + list(task.get("cleanup_actions", []) or [])
            for action in task_actions:
                if action.get("kind") in {"sql_workload", "raw_sql_workload"}:
                    sql = action.get("sql", "")
                    _append_dangerous_sql_reasons(reasons, task_id, sql)
                elif action.get("kind") == "raw_transaction_script":
                    for script in action.get("scripts", []) or []:
                        if not isinstance(script, dict):
                            continue
                        for step in script.get("steps", []) or []:
                            sql = step if isinstance(step, str) else (step or {}).get("sql", "")
                            _append_dangerous_sql_reasons(reasons, task_id, str(sql or ""))
                elif action.get("kind") == "logical_backup":
                    tool = action.get("tool", "")
                    if not tool:
                        warnings.append(f"Task '{task_id}' backup has no tool specified")
                elif action.get("kind") == "benchbase_burst_command":
                    command = action.get("command")
                    if not isinstance(command, list):
                        reasons.append(f"Task '{task_id}' benchbase_burst_command command must be argv array")
                    else:
                        _append_benchbase_burst_command_reasons(
                            reasons,
                            task_id,
                            action,
                            expected_workload,
                        )
                    if expected_workload:
                        expected_benchmark = str(expected_workload.get("benchmark") or "").lower()
                        benchmark = str(action.get("benchmark") or "").lower()
                        if expected_benchmark and benchmark and benchmark != expected_benchmark:
                            reasons.append(
                                f"Task '{task_id}' benchmark {benchmark} does not match "
                                f"background workload {expected_benchmark}"
                            )
                        expected_database = str(expected_workload.get("database") or "")
                        database = str(action.get("database") or "")
                        if expected_database and database and database != expected_database:
                            reasons.append(
                                f"Task '{task_id}' database {database} does not match "
                                f"background workload {expected_database}"
                            )
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
                cmd_value = action.get("command", "")
                cmd = " ".join(cmd_value) if isinstance(cmd_value, list) else str(cmd_value)
                if action.get("kind") in {"raw_command", "logical_backup_command"} and not isinstance(cmd_value, list):
                    reasons.append(f"Task '{task_id}' command must be argv array")
                if _is_resource_task(task):
                    _append_resource_chaosblade_reasons(
                        reasons,
                        task_id,
                        action,
                        self.config.chaosblade_path,
                    )
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
                if action.get("kind") in {"chaosblade", "raw_command"}:
                    dur = action.get("duration_sec", 0)
                    if dur > duration_limit:
                        reasons.append(
                            f"Action duration {dur}s in task '{task_id}' "
                            f"exceeds max {duration_limit}s"
                        )

        # -------------------------------------------------------------------------
        # Overall decision
        # -------------------------------------------------------------------------
        approved = len(reasons) == 0
        return SafetyResult(approved=approved, reasons=reasons, warnings=warnings)


def _append_dangerous_sql_reasons(reasons: list[str], task_id: str, sql: str) -> None:
    for pat in _DANGEROUS_SQL_PATTERNS:
        if pat.search(sql):
            reasons.append(
                f"Task '{task_id}' contains dangerous SQL pattern: "
                f"{pat.pattern}"
            )


def _append_benchbase_burst_command_reasons(
    reasons: list[str],
    task_id: str,
    action: dict[str, Any],
    expected_workload: dict[str, Any] | None,
) -> None:
    command = action.get("command") or []
    if not command:
        reasons.append(f"Task '{task_id}' benchbase_burst_command command cannot be empty")
        return
    cwd = action.get("cwd")
    executable = str(command[0])
    executable_name = Path(executable).name.lower()
    if executable_name != "java":
        reasons.append(
            f"Task '{task_id}' benchbase_burst_command must invoke java/java_bin as command[0], "
            f"got {executable!r}"
        )
    elif not _executable_exists(executable, cwd):
        reasons.append(f"Task '{task_id}' benchbase_burst_command java executable not found: {executable}")

    if "-jar" not in command:
        reasons.append(f"Task '{task_id}' benchbase_burst_command must use java -jar benchbase.jar")
        return
    jar_idx = command.index("-jar") + 1
    if jar_idx >= len(command):
        reasons.append(f"Task '{task_id}' benchbase_burst_command missing jar path after -jar")
        return
    jar_path = _resolve_maybe_relative(str(command[jar_idx]), cwd)
    if not jar_path.exists() or not jar_path.is_file():
        reasons.append(f"Task '{task_id}' benchbase_burst_command jar not found: {command[jar_idx]}")
    elif jar_path.name != "benchbase.jar":
        reasons.append(f"Task '{task_id}' benchbase_burst_command jar must be benchbase.jar")

    if "-b" in command:
        b_idx = command.index("-b") + 1
        if b_idx < len(command):
            benchmark_arg = str(command[b_idx]).lower()
            expected_benchmark = str((expected_workload or {}).get("benchmark") or action.get("benchmark") or "").lower()
            if expected_benchmark and benchmark_arg != expected_benchmark:
                reasons.append(
                    f"Task '{task_id}' benchbase command -b {benchmark_arg} does not match expected {expected_benchmark}"
                )
    else:
        reasons.append(f"Task '{task_id}' benchbase_burst_command must include -b benchmark")

    if "-c" in command:
        c_idx = command.index("-c") + 1
        if c_idx < len(command):
            config_path = _resolve_maybe_relative(str(command[c_idx]), cwd)
            if not config_path.exists() or not config_path.is_file():
                reasons.append(f"Task '{task_id}' benchbase_burst_command config not found: {command[c_idx]}")
    else:
        reasons.append(f"Task '{task_id}' benchbase_burst_command must include -c config_path")


def _resolve_maybe_relative(path: str, cwd: Any = None) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    if cwd:
        return (Path(str(cwd)).expanduser() / p).resolve()
    return (Path.cwd() / p).resolve()


def _executable_exists(executable: str, cwd: Any = None) -> bool:
    p = Path(executable).expanduser()
    if p.is_absolute() or len(p.parts) > 1:
        resolved = _resolve_maybe_relative(executable, cwd)
        return resolved.exists() and resolved.is_file()
    return shutil.which(executable) is not None


def _is_resource_task(task: dict[str, Any]) -> bool:
    root = str((task.get("metadata") or {}).get("root_cause") or "")
    return root in RESOURCE_ROOT_CAUSES


def _append_resource_chaosblade_reasons(
    reasons: list[str],
    task_id: str,
    action: dict[str, Any],
    configured_path: str,
) -> None:
    if action.get("kind") != "raw_command":
        reasons.append(f"Task '{task_id}' resource action must use raw_command")
        return
    command = action.get("command")
    cleanup = action.get("cleanup_command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        reasons.append(f"Task '{task_id}' resource ChaosBlade command must be argv array")
        return
    try:
        duration = float(action.get("duration_sec", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        reasons.append(f"Task '{task_id}' resource ChaosBlade duration_sec must be greater than 0")
    if len(command) < 3:
        reasons.append(f"Task '{task_id}' resource ChaosBlade command is too short")
        return
    if not _is_chaosblade_binary(command[0], configured_path):
        reasons.append(f"Task '{task_id}' resource command must invoke RuntimeConfig.chaosblade_path, not bare 'blade'")
    if command[1] != "create":
        reasons.append(f"Task '{task_id}' resource ChaosBlade command must use create")
    if command[2] not in CHAOSBLADE_RESOURCE_TARGETS:
        reasons.append(
            f"Task '{task_id}' resource ChaosBlade target must be one of "
            f"{sorted(CHAOSBLADE_RESOURCE_TARGETS)}"
        )
    _append_darwin_chaosblade_reasons(reasons, task_id, command)
    uid = _extract_chaosblade_uid(command)
    if not uid:
        reasons.append(f"Task '{task_id}' resource ChaosBlade command must include --uid")
    if not isinstance(cleanup, list) or not all(isinstance(part, str) for part in cleanup):
        reasons.append(f"Task '{task_id}' resource cleanup_command must be argv array")
        return
    if len(cleanup) < 3:
        reasons.append(f"Task '{task_id}' resource cleanup_command is too short")
        return
    if not _is_chaosblade_binary(cleanup[0], configured_path):
        reasons.append(f"Task '{task_id}' resource cleanup_command must invoke RuntimeConfig.chaosblade_path, not bare 'blade'")
    if "destroy" not in cleanup:
        reasons.append(f"Task '{task_id}' resource cleanup_command must destroy the experiment")
    if uid and uid not in cleanup:
        reasons.append(f"Task '{task_id}' resource cleanup_command must contain the same ChaosBlade uid")


def _append_darwin_chaosblade_reasons(reasons: list[str], task_id: str, command: list[str]) -> None:
    if len(command) < 4:
        reasons.append(f"Task '{task_id}' resource ChaosBlade command must include a Darwin-supported subcommand")
        return
    forbidden = {"--duration", "--process-name", "--read-bps", "--write-bps"}
    used_forbidden = sorted(part for part in command if part in forbidden)
    if used_forbidden:
        reasons.append(
            f"Task '{task_id}' resource ChaosBlade command uses unsupported flags: {used_forbidden}"
        )
    target = command[2]
    subcommand = command[3]
    if target == "cpu" and subcommand != "fullload":
        reasons.append(f"Task '{task_id}' resource_cpu must use 'cpu fullload'")
    if target == "mem":
        if subcommand != "load":
            reasons.append(f"Task '{task_id}' resource_memory must use 'mem load'")
        if "--mode" not in command:
            reasons.append(f"Task '{task_id}' resource_memory must include --mode")
    if target == "disk":
        if subcommand != "burn":
            reasons.append(f"Task '{task_id}' resource_io must use 'disk burn'")
        if "--read" not in command or "--write" not in command:
            reasons.append(f"Task '{task_id}' resource_io must include --read and --write")
    if target == "network":
        if subcommand != "drop":
            reasons.append(f"Task '{task_id}' resource_network must use Darwin-supported 'network drop'")
        if not any(flag in command for flag in ("--destination-port", "--source-port", "--destination-ip", "--source-ip", "--string-pattern")):
            reasons.append(f"Task '{task_id}' resource_network must scope dropped traffic by port, ip, or string pattern")
        if "--network-traffic" not in command:
            reasons.append(f"Task '{task_id}' resource_network must include --network-traffic")
    if target in {"cpu", "mem", "disk", "network"} and "--timeout" not in command:
        reasons.append(f"Task '{task_id}' resource ChaosBlade command must include --timeout")


def _is_chaosblade_binary(value: str, configured_path: str) -> bool:
    if not value:
        return False
    return value == configured_path


def _extract_chaosblade_uid(command: list[str]) -> str:
    for idx, part in enumerate(command):
        if part == "--uid" and idx + 1 < len(command):
            return command[idx + 1]
        if part.startswith("--uid="):
            return part.split("=", 1)[1]
    return ""


def action_duration_sec(action: dict[str, Any]) -> float:
    if action.get("kind") == "benchbase_burst":
        profile = action.get("profile") or {}
        return float(profile.get("duration_sec", action.get("duration_sec", 0)) or 0)
    if action.get("kind") in {"benchbase_burst_command", "raw_command", "raw_sql_workload", "raw_transaction_script", "logical_backup_command"}:
        return float(action.get("duration_sec", 0) or 0)
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
    if kind == "benchbase_burst_command":
        return int(action.get("terminals", 0) or 0)
    if kind in {"raw_sql_workload", "raw_transaction_script"}:
        if kind == "raw_transaction_script":
            scripts = action.get("scripts") or []
            if isinstance(scripts, list):
                total = 0
                for script in scripts:
                    if isinstance(script, dict):
                        total += int(script.get("concurrency", action.get("concurrency", 1)) or 1)
                return total
        return int(action.get("concurrency", 0) or 0)
    if kind == "workload_ramp":
        stages = action.get("ramp_stages") or []
        if isinstance(stages, list):
            return max((int((stage or {}).get("connections", 0) or 0) for stage in stages if isinstance(stage, dict)), default=0)
        return 0
    return int(action.get("concurrency", 0) or 0)


def action_grace_sec(action: dict[str, Any]) -> float:
    if action.get("kind") in {
        "benchbase_burst",
        "sql_workload",
        "chaosblade",
        "lock_conflict",
        "logical_backup",
        "raw_sql_workload",
        "raw_transaction_script",
        "raw_command",
        "logical_backup_command",
        "benchbase_burst_command",
    }:
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
