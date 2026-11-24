"""Read-only checks required before starting a real experiment."""

from __future__ import annotations

import re
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import ExperimentProfile


class PreflightLevel(StrEnum):
    WARN = "WARN"
    FAIL = "FAIL"


class PreflightIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    level: PreflightLevel
    message: str


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    issues: list[PreflightIssue] = Field(default_factory=list)


def run_preflight(adapter, profile: ExperimentProfile, *, chaosblade_path: str | Path | None = None) -> PreflightResult:
    """Run checks without changing MySQL or starting any workload."""
    issues: list[PreflightIssue] = []

    def fail(check: str, message: str) -> None:
        issues.append(PreflightIssue(check=check, level=PreflightLevel.FAIL, message=message))

    def warn(check: str, message: str) -> None:
        issues.append(PreflightIssue(check=check, level=PreflightLevel.WARN, message=message))

    connectivity = adapter.execute_readonly("SELECT 1 AS value")
    if not connectivity.success:
        fail("mysql_connection", connectivity.error_message or "MySQL connection failed")
        return PreflightResult(passed=False, issues=issues)

    status = adapter.execute_readonly("SHOW GLOBAL STATUS LIKE 'Questions'")
    if not status.success:
        fail("global_status", status.error_message or "SHOW GLOBAL STATUS is unavailable")

    performance_schema = adapter.execute_readonly("SHOW VARIABLES LIKE 'performance_schema'")
    if not performance_schema.success:
        fail("performance_schema", performance_schema.error_message or "performance_schema check failed")
    elif not _is_on(performance_schema.data):
        fail("performance_schema", "performance_schema is OFF")

    _check_instrumentation(adapter, profile, warn)

    if profile.snapshot.data_lock_waits_supporting:
        lock_waits = adapter.execute_readonly("SELECT * FROM performance_schema.data_lock_waits LIMIT 1")
        if not lock_waits.success:
            warn("data_lock_waits", lock_waits.error_message or "data_lock_waits is unavailable")

    if profile.workload.runner != "benchbase":
        fail("workload.runner", f"unsupported workload runner: {profile.workload.runner}")
    observation_seconds = profile.experiment.observe_seconds or profile.experiment.injection_seconds
    required_duration = (
        profile.experiment.warmup_seconds
        + profile.experiment.baseline_seconds
        + observation_seconds
        + profile.experiment.recovery_seconds
    )
    if profile.workload.duration_seconds < required_duration:
        fail("workload.duration_seconds", "Background workload duration is shorter than the experiment window")

    benchbase = profile.benchbase
    if not benchbase.jar_path.is_file():
        fail("benchbase.jar", f"BenchBase JAR does not exist: {benchbase.jar_path}")
    if not benchbase.config_dir.is_dir():
        fail("benchbase.config_dir", f"BenchBase config directory does not exist: {benchbase.config_dir}")
    _check_java(benchbase.java_bin, benchbase.minimum_java_major_version, fail)

    if chaosblade_path is not None:
        blade = Path(chaosblade_path).expanduser()
        if not blade.is_absolute():
            blade = Path.cwd() / blade
        if not blade.is_file() or not blade.stat().st_mode & 0o111:
            fail("chaosblade", f"ChaosBlade is missing or not executable: {blade.resolve()}")

    return PreflightResult(
        passed=not any(issue.level == PreflightLevel.FAIL for issue in issues),
        issues=issues,
    )


def _check_instrumentation(adapter, profile: ExperimentProfile, warn) -> None:
    variables = {}
    for name in ("slow_query_log", "long_query_time", "log_output"):
        result = adapter.execute_readonly(f"SHOW VARIABLES LIKE '{name}'")
        if not result.success or not result.data:
            warn(name, result.error_message or f"unable to read {name}")
            continue
        variables[name] = str(result.data[0].get("Value", ""))
    if variables.get("slow_query_log", "").upper() != ("ON" if profile.instrumentation.slow_query_log else "OFF"):
        warn("slow_query_log", "current value differs from experiment profile; prepare phase will apply it")
    try:
        current_long_query_time = float(variables.get("long_query_time", "nan"))
        if current_long_query_time != profile.instrumentation.long_query_time_seconds:
            warn("long_query_time", "current value differs from experiment profile; prepare phase will apply it")
    except ValueError:
        warn("long_query_time", "current value is not numeric")
    if variables.get("log_output", "").upper() != profile.instrumentation.log_output.upper():
        warn("log_output", "current value differs from experiment profile; prepare phase will apply it")


def _check_java(java_bin: str, minimum_major: int, fail) -> None:
    if Path(java_bin).is_absolute() and not Path(java_bin).is_file():
        fail("java", f"Java executable does not exist: {java_bin}")
        return
    if not Path(java_bin).is_absolute() and shutil.which(java_bin) is None:
        fail("java", f"Java executable is not on PATH: {java_bin}")
        return
    try:
        result = subprocess.run([java_bin, "-version"], capture_output=True, text=True, timeout=5, check=False)
        output = f"{result.stdout}\n{result.stderr}"
        match = re.search(r'version\s+"(\d+)', output)
        if match and int(match.group(1)) < minimum_major:
            fail("java", f"Java {minimum_major} or newer is required, found {match.group(1)}")
        elif result.returncode != 0:
            fail("java", output.strip() or "java -version failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail("java", str(exc))


def _is_on(rows) -> bool:
    return bool(rows and str(rows[0].get("Value", "")).upper() in {"ON", "1"})
