from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__).resolve()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".env").exists() or (candidate / ".git").exists():
            return candidate
    return Path.cwd()


def load_env_file(path: Path | None = None) -> dict[str, str]:
    env_path = path or (find_repo_root() / ".env")
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def get_env(name: str, default: str | None = None) -> str | None:
    load_env_file()
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = get_env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = get_env(name)
    if value in {None, ""}:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = get_env(name)
    if value in {None, ""}:
        return default
    return float(value)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str | None
    base_url: str
    model: str
    planner_temperature: float
    sql_temperature: float
    enable_planner: bool
    enable_sql: bool

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            api_key=get_env("OPENAI_API_KEY"),
            base_url=(get_env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/"),
            model=get_env("OPENAI_MODEL", "gpt-4o") or "gpt-4o",
            planner_temperature=env_float("OPENAI_PLANNER_TEMPERATURE", 0.2),
            sql_temperature=env_float("OPENAI_SQL_TEMPERATURE", 0.1),
            enable_planner=env_bool("ENABLE_OPENAI_PLANNER", False),
            enable_sql=env_bool("ENABLE_OPENAI_SQL", False),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "api_key_configured": bool(self.api_key),
            "base_url": self.base_url,
            "model": self.model,
            "planner_temperature": self.planner_temperature,
            "sql_temperature": self.sql_temperature,
            "enable_planner": self.enable_planner,
            "enable_sql": self.enable_sql,
        }


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    default_database: str
    max_concurrency: int
    server_address: str | None
    server_username: str | None
    server_password: str | None
    chaosblade_path: str | None

    @classmethod
    def from_env(cls) -> "MySQLConfig":
        database = get_env("DBMAGS_MYSQL_DB", "tpcc10_test") or "tpcc10_test"
        return cls(
            host=get_env("DBMAGS_MYSQL_HOST", "127.0.0.1") or "127.0.0.1",
            port=env_int("DBMAGS_MYSQL_PORT", 3306),
            user=get_env("DBMAGS_MYSQL_USER", "root") or "root",
            password=get_env("DBMAGS_MYSQL_PASSWORD", "") or "",
            database=database,
            default_database=get_env("DBMAGS_DEFAULT_DATABASE", database) or database,
            max_concurrency=env_int("DBMAGS_MAX_CONCURRENCY", 100),
            server_address=get_env("DBMAGS_SERVER_ADDRESS"),
            server_username=get_env("DBMAGS_SERVER_USERNAME"),
            server_password=get_env("DBMAGS_SERVER_PASSWORD"),
            chaosblade_path=get_env("DBMAGS_CHAOSBLADE_PATH"),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password_configured": bool(self.password),
            "database": self.database,
            "default_database": self.default_database,
            "max_concurrency": self.max_concurrency,
            "server_address": self.server_address,
            "server_username": self.server_username,
            "server_password_configured": bool(self.server_password),
            "chaosblade_path": self.chaosblade_path,
        }
