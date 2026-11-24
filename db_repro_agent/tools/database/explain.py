"""EXPLAIN probe for MySQL SELECT statements."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Mapping, Sequence

from .base import ToolResult, utc_now, validate_readonly_sql


class ExplainProbe:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def explain(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> ToolResult:
        started = utc_now()
        try:
            validate_readonly_sql(sql)
            with closing(self.adapter.connect()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN " + sql.strip().rstrip(";"), params)
                    rows = list(cursor.fetchall())
            return ToolResult.ok(rows, started, utc_now())
        except Exception as exc:
            from .mysql import _error_result

            return _error_result(exc, started, utc_now())
