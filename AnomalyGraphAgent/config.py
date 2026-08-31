"""
Runtime configuration loaded from environment variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_type_hints


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_runtime_path(value: str | Path) -> str:
    """Resolve a configured repository-relative path to an absolute path."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Parse a .env file and return key-value pairs."""
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Handle "export KEY=value" and "KEY=value"
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            # Strip quotes
            if len(val) >= 2 and val[0] == val[-1] in ('"', "'"):
                val = val[1:-1]
            env[key] = val
    return env


@dataclass
class RuntimeConfig:
    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    planner_temperature: float = 0.2
    planner_enabled: bool = True
    input_analysis_llm_timeout_sec: int = 180
    input_analysis_llm_max_attempts: int = 2

    # MySQL
    default_database: str = "testdb"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""

    # Local experiment executables
    benchbase_jar_path: str = ".tools/benchbase-main/target/benchbase-mysql/benchbase.jar"
    benchbase_java_bin: str = "/opt/homebrew/opt/openjdk/bin/java"
    chaosblade_path: str = ".tools/chaosblade-1.8.0-darwin_arm64/blade"

    # Safety limits
    max_connection_usage_ratio: float = 0.8
    max_cpu_usage: float = 90.0
    max_duration_sec: int = 300
    max_retry_rounds: int = 5

    # Memory
    memory_file: str = "memory/long_term_memory.jsonl"

    def __post_init__(self) -> None:
        self.benchbase_jar_path = resolve_runtime_path(self.benchbase_jar_path)
        self.chaosblade_path = resolve_runtime_path(self.chaosblade_path)
        java_path = Path(self.benchbase_java_bin).expanduser()
        if java_path.is_absolute() or len(java_path.parts) > 1:
            self.benchbase_java_bin = resolve_runtime_path(java_path)

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> RuntimeConfig:
        env = {}
        for p in [dotenv_path, ".env.local"]:
            env.update(_load_dotenv(p))
        aliases = {
            "OPENAI_API_KEY": "openai_api_key",
            "OPENAI_BASE_URL": "openai_base_url",
            "OPENAI_MODEL": "openai_model",
            "OPENAI_PLANNER_TEMPERATURE": "planner_temperature",
            "ENABLE_OPENAI_PLANNER": "planner_enabled",
            "INPUT_ANALYSIS_LLM_TIMEOUT_SEC": "input_analysis_llm_timeout_sec",
            "INPUT_ANALYSIS_LLM_MAX_ATTEMPTS": "input_analysis_llm_max_attempts",
            "DBMAGS_DEFAULT_DATABASE": "default_database",
            "DBMAGS_MYSQL_HOST": "mysql_host",
            "DBMAGS_MYSQL_PORT": "mysql_port",
            "DBMAGS_MYSQL_USER": "mysql_user",
            "DBMAGS_MYSQL_PASSWORD": "mysql_password",
            "DBMAGS_BENCHBASE_JAR_PATH": "benchbase_jar_path",
            "DBMAGS_BENCHBASE_JAVA_BIN": "benchbase_java_bin",
            "DBMAGS_CHAOSBLADE_PATH": "chaosblade_path",
            "DBMAGS_MAX_RETRY_ROUNDS": "max_retry_rounds",
        }
        for source, target in aliases.items():
            if source in env and target not in env:
                env[target] = env[source]
        # Also read from os.environ (os.environ takes precedence)
        for source, target in aliases.items():
            env_val = os.environ.get(source)
            if env_val is not None:
                env[target] = env_val
        for key in cls.__dataclass_fields__:
            env_val = os.environ.get(key)
            if env_val is not None:
                env[key] = env_val
        kwargs = {}
        type_hints = get_type_hints(cls)
        for key, field_ in cls.__dataclass_fields__.items():
            if key in env:
                val = env[key]
                t = type_hints.get(key, field_.type)
                if t in (bool,):
                    kwargs[key] = val.lower() in ("1", "true", "yes")
                elif t in (int,):
                    kwargs[key] = int(val)
                elif t in (float,):
                    kwargs[key] = float(val)
                else:
                    kwargs[key] = val
        return cls(**kwargs)
