"""Database Tool Layer protocols and the common result envelope."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict


class ToolResult(BaseModel):
    """Stable result protocol for every database tool call."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    data: Any = None
    error_code: int | str | None = None
    error_message: str | None = None
    duration_ms: float
    started_at: datetime
    finished_at: datetime
    error_type: str | None = None
    error_details: dict[str, Any] | None = None

    @classmethod
    def ok(cls, data: Any, started_at: datetime, finished_at: datetime) -> "ToolResult":
        return cls(
            success=True,
            data=data,
            duration_ms=_duration_ms(started_at, finished_at),
            started_at=started_at,
            finished_at=finished_at,
        )

    @classmethod
    def failure(
        cls,
        *,
        started_at: datetime,
        finished_at: datetime,
        error_code: int | str | None,
        error_message: str,
        error_type: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            success=False,
            data=None,
            error_code=error_code,
            error_message=error_message,
            duration_ms=_duration_ms(started_at, finished_at),
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
            error_details=error_details,
        )


class DatabaseTool(ABC):
    """Backend-neutral database operations required by later Agent modules."""

    @abstractmethod
    def probe_schema(self, database: str) -> ToolResult: ...

    @abstractmethod
    def probe_indexes(self, database: str, table: str) -> ToolResult: ...

    @abstractmethod
    def probe_row_counts(self, database: str) -> ToolResult: ...

    @abstractmethod
    def probe_runtime(self) -> ToolResult: ...

    @abstractmethod
    def execute_readonly(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> ToolResult: ...

    @abstractmethod
    def explain_sql(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> ToolResult: ...


def validate_readonly_sql(sql: str) -> None:
    """Reject mutation, multi-statement and file-export SQL before execution."""

    normalized = _strip_leading_comments(sql).strip()
    if not normalized:
        raise ValueError("SQL must not be empty")
    if ";" in normalized.rstrip(";"):
        raise ValueError("multiple SQL statements are not allowed")
    if not re.match(r"^(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN|WITH)\b", normalized, re.IGNORECASE):
        raise ValueError("only SELECT, SHOW, DESCRIBE, DESC, EXPLAIN and WITH are allowed")
    if re.search(r"\bINTO\s+(OUTFILE|DUMPFILE)\b", normalized, re.IGNORECASE):
        raise ValueError("SELECT INTO OUTFILE/DUMPFILE is not allowed")


def _strip_leading_comments(sql: str) -> str:
    value = sql.lstrip()
    while value.startswith("--"):
        newline = value.find("\n")
        value = "" if newline < 0 else value[newline + 1 :].lstrip()
    while value.startswith("/*"):
        end = value.find("*/", 2)
        value = "" if end < 0 else value[end + 2 :].lstrip()
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _duration_ms(started_at: datetime, finished_at: datetime) -> float:
    return round((finished_at - started_at).total_seconds() * 1000, 3)
