"""MySQL implementation of the database Tool Layer."""

from __future__ import annotations

import os
import re
from contextlib import closing
from datetime import datetime
from typing import Any, Mapping, Sequence

import pymysql
from pydantic import BaseModel, ConfigDict, Field

from .base import DatabaseTool, ToolResult, validate_readonly_sql, utc_now
from .explain import ExplainProbe
from .runtime_probe import RuntimeProbe
from .schema_probe import SchemaProbe


class MySQLConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = "root"
    password: str = ""
    database: str = "tpcc10_test"
    connect_timeout: float = Field(default=5.0, gt=0)
    charset: str = "utf8mb4"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MySQLConfig":
        env = environ if environ is not None else os.environ
        return cls(
            host=env.get("DBMAGS_MYSQL_HOST", env.get("DBMAGS_SERVER_ADDRESS", "127.0.0.1")),
            port=int(env.get("DBMAGS_MYSQL_PORT", "3306")),
            user=env.get("DBMAGS_MYSQL_USER", env.get("DBMAGS_SERVER_USERNAME", "root")),
            password=env.get("DBMAGS_MYSQL_PASSWORD", env.get("DBMAGS_SERVER_PASSWORD", "")),
            database=env.get("DBMAGS_MYSQL_DB", env.get("DBMAGS_DEFAULT_DATABASE", "tpcc10_test")),
            connect_timeout=float(env.get("DBMAGS_MYSQL_CONNECT_TIMEOUT", "5")),
        )


class MySQLAdapter(DatabaseTool):
    def __init__(self, config: MySQLConfig | None = None) -> None:
        self.config = config or MySQLConfig.from_env()
        self.schema_probe = SchemaProbe(self)
        self.runtime_probe = RuntimeProbe(self)
        self.explain_probe = ExplainProbe(self)

    def connect(self, *, database: str | None = None):
        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=database or self.config.database,
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.connect_timeout,
            write_timeout=self.config.connect_timeout,
            charset=self.config.charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def probe_schema(self, database: str | None = None) -> ToolResult:
        return self.schema_probe.probe_schema(database or self.config.database)

    def probe_indexes(self, database: str, table: str) -> ToolResult:
        return self.schema_probe.probe_indexes(database, table)

    def probe_row_counts(self, database: str | None = None) -> ToolResult:
        return self.schema_probe.probe_row_counts(database or self.config.database)

    def probe_runtime(self) -> ToolResult:
        return self.runtime_probe.probe_runtime()

    def execute_readonly(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> ToolResult:
        started = utc_now()
        try:
            validate_readonly_sql(sql)
            with closing(self.connect()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
            finished = utc_now()
            return ToolResult.ok(list(rows), started, finished)
        except Exception as exc:  # preserve every DB and validation error in the envelope
            finished = utc_now()
            return _error_result(exc, started, finished)

    def explain_sql(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> ToolResult:
        return self.explain_probe.explain(sql, params)

    def close(self) -> None:
        """Compatibility hook; connections are scoped and closed per operation."""


def _error_result(exc: Exception, started: datetime, finished: datetime) -> ToolResult:
    error_code: int | str | None = None
    error_details: dict[str, Any] = {}
    if isinstance(exc, pymysql.MySQLError):
        if exc.args:
            error_code = exc.args[0] if isinstance(exc.args[0], (int, str)) else str(exc.args[0])
        error_details["mysql_args"] = [str(arg) for arg in exc.args]
    return ToolResult.failure(
        started_at=started,
        finished_at=finished,
        error_code=error_code or type(exc).__name__,
        error_message=str(exc),
        error_type=type(exc).__name__,
        error_details=error_details or None,
    )
