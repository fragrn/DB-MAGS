"""Typed configuration loading for local and batch experiments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models.common import ErrorCode, ReproductionError


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = "root"
    password: str = ""
    database: str = "tpcc10_test"
    connect_timeout_seconds: float = Field(default=5.0, gt=0)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    api_mode: str = Field(default="responses", pattern="^(responses|chat_completions)$")
    request_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    max_retries: int = Field(default=1, ge=0, le=3)


class BenchBaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jar_path: Path = Path(".tools/benchbase-main/target/benchbase-mysql/benchbase.jar")
    config_dir: Path = Path(".tools/benchbase-main/target/benchbase-mysql/config/mysql")
    config_path: Path | None = None
    results_dir: Path = Path(".tools/benchbase-main/target/benchbase-mysql/results")
    java_bin: str = "java"
    minimum_java_major_version: int = Field(default=23, ge=8)


class ExperimentWindowConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    warmup_seconds: int = Field(default=10, ge=0)
    baseline_seconds: int = Field(default=30, ge=1)
    injection_seconds: int = Field(default=60, ge=1)
    observe_seconds: int | None = Field(default=None, ge=1)
    recovery_seconds: int = Field(default=20, ge=0)
    sample_interval_seconds: float = Field(default=1.0, gt=0)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_attempts: int = Field(default=3, ge=1)
    max_hitl_rounds: int = Field(default=2, ge=0)


class WorkloadProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner: str = "benchbase"
    benchmark: str = "tpcc"
    terminals: int = Field(default=4, ge=1)
    rate: int = Field(default=10, ge=1)
    duration_seconds: int = Field(default=180, ge=1)


class AgentLimitsProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_action_duration_seconds: int = Field(default=15, ge=1)
    max_concurrency: int = Field(default=8, ge=1)
    max_retry_rounds: int = Field(default=2, ge=0)


class InstrumentationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slow_query_log: bool = True
    long_query_time_seconds: float = Field(default=1.0, ge=0)
    log_output: str = "FILE"


class SnapshotProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    data_lock_waits_supporting: bool = True


class ExperimentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment: ExperimentWindowConfig = Field(default_factory=ExperimentWindowConfig)
    workload: WorkloadProfile = Field(default_factory=WorkloadProfile)
    benchbase: BenchBaseConfig = Field(default_factory=BenchBaseConfig)
    agent_limits: AgentLimitsProfile = Field(default_factory=AgentLimitsProfile)
    instrumentation: InstrumentationProfile = Field(default_factory=InstrumentationProfile)
    snapshot: SnapshotProfile = Field(default_factory=SnapshotProfile)

    def resolve_paths(self, base_dir: Path | None = None) -> "ExperimentProfile":
        base = (base_dir or Path.cwd()).resolve()
        values = self.model_dump()
        benchbase = dict(values["benchbase"])
        for key in ("jar_path", "config_dir", "config_path", "results_dir"):
            if benchbase[key] is None:
                continue
            benchbase[key] = _resolve_path(benchbase[key], base)
        values["benchbase"] = benchbase
        return type(self).model_validate(values)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    benchbase: BenchBaseConfig = Field(default_factory=BenchBaseConfig)
    windows: ExperimentWindowConfig = Field(default_factory=ExperimentWindowConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    artifact_root: Path = Path("experiment_runs")

    def resolve_paths(self, base_dir: Path | None = None) -> "AppConfig":
        base = (base_dir or Path.cwd()).resolve()
        values = self.model_dump()
        values["artifact_root"] = _resolve_path(self.artifact_root, base)
        benchbase = dict(values["benchbase"])
        for key in ("jar_path", "config_dir", "config_path", "results_dir"):
            if benchbase[key] is None:
                continue
            benchbase[key] = _resolve_path(benchbase[key], base)
        values["benchbase"] = benchbase
        return type(self).model_validate(values)


def load_config(path: str | Path | None = None, *, environ: dict[str, str] | None = None) -> AppConfig:
    """Load YAML/JSON configuration, then apply DBMAGS_* environment overrides."""

    # Explicit mappings are used by tests and callers that already resolved
    # their environment. For normal execution, load the repository-local
    # .env first and let real process variables take precedence.
    env = environ if environ is not None else {**_read_dotenv(Path(".env")), **os.environ}
    raw: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise ReproductionError(
                f"Configuration file does not exist: {config_path}", code=ErrorCode.CONFIGURATION
            )
        try:
            raw = _read_mapping_file(config_path)
        except (OSError, ValueError) as exc:
            raise ReproductionError(
                f"Unable to read configuration file: {config_path}",
                code=ErrorCode.CONFIGURATION,
                details={"error": str(exc)},
            ) from exc

    raw = _deep_merge(raw, _environment_overrides(env))
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ReproductionError(
            "Invalid application configuration",
            code=ErrorCode.CONFIGURATION,
            details={"errors": exc.errors()},
        ) from exc


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without exposing values in logs."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_experiment_profile(path: str | Path) -> ExperimentProfile:
    """Load the human-adjustable experiment runtime profile."""
    profile_path = Path(path)
    if not profile_path.exists():
        raise ReproductionError(
            f"Experiment profile does not exist: {profile_path}", code=ErrorCode.CONFIGURATION
        )
    try:
        raw = _read_mapping_file(profile_path)
        return ExperimentProfile.model_validate(raw).resolve_paths(profile_path.parent.parent)
    except (OSError, ValueError, ValidationError) as exc:
        raise ReproductionError(
            f"Invalid experiment profile: {profile_path}",
            code=ErrorCode.CONFIGURATION,
            details={"error": str(exc)},
        ) from exc


def _read_mapping_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        value = _parse_simple_yaml(text)
    if not isinstance(value, dict):
        raise ValueError("configuration root must be a mapping")
    return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by local configuration files.

    PyYAML remains the declared dependency. This fallback keeps `doctor` usable in a
    minimal checkout before optional dependencies are installed.
    """

    try:
        import yaml  # type: ignore
    except ImportError:
        return _parse_indented_mapping(text)
    value = yaml.safe_load(text)
    return value or {}


def _parse_indented_mapping(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in line:
            raise ValueError(f"unsupported YAML line: {raw_line}")
        key, raw_value = line.strip().split(":", 1)
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        value = raw_value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str) -> Any:
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none"}:
        return None
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _environment_overrides(env: dict[str, str]) -> dict[str, Any]:
    mapping: dict[str, tuple[str, str, Any]] = {
        "DBMAGS_MYSQL_HOST": ("database", "host", str),
        "DBMAGS_MYSQL_PORT": ("database", "port", int),
        "DBMAGS_MYSQL_USER": ("database", "user", str),
        "DBMAGS_MYSQL_PASSWORD": ("database", "password", str),
        "DBMAGS_MYSQL_DB": ("database", "database", str),
        "DBMAGS_SERVER_ADDRESS": ("database", "host", str),
        "DBMAGS_SERVER_USERNAME": ("database", "user", str),
        "DBMAGS_SERVER_PASSWORD": ("database", "password", str),
        "DBMAGS_DEFAULT_DATABASE": ("database", "database", str),
        "OPENAI_BASE_URL": ("llm", "base_url", str),
        "OPENAI_API_KEY": ("llm", "api_key", str),
        "OPENAI_MODEL": ("llm", "model", str),
        "OPENAI_API_MODE": ("llm", "api_mode", str),
        "OPENAI_REQUEST_TIMEOUT_SECONDS": ("llm", "request_timeout_seconds", float),
        "OPENAI_MAX_RETRIES": ("llm", "max_retries", int),
        "DBMAGS_BENCHBASE_JAR": ("benchbase", "jar_path", str),
        "DBMAGS_ARTIFACT_ROOT": ("_root", "artifact_root", str),
        "DBMAGS_MAX_ATTEMPTS": ("agent", "max_attempts", int),
        "DBMAGS_MAX_HITL_ROUNDS": ("agent", "max_hitl_rounds", int),
    }
    result: dict[str, Any] = {}
    for name, (section, field, converter) in mapping.items():
        if name not in env:
            continue
        try:
            converted = converter(env[name])
        except ValueError as exc:
            raise ReproductionError(
                f"Invalid environment variable: {name}",
                code=ErrorCode.CONFIGURATION,
                details={"value": "<redacted>" if "PASSWORD" in name or "KEY" in name else env[name]},
            ) from exc
        if section == "_root":
            result[field] = converted
        else:
            result.setdefault(section, {})[field] = converted
    return result


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_path(value: Path | str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()
