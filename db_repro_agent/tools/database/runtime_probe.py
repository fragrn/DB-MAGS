"""MySQL runtime status probes."""

from __future__ import annotations

from contextlib import closing

from .base import ToolResult, utc_now


class RuntimeProbe:
    STATUS_NAMES = (
        "Threads_connected",
        "Threads_running",
        "Slow_queries",
        "Connections",
        "Questions",
        "Threads_created",
        "Aborted_connects",
        "Aborted_clients",
        "Connection_errors_accept",
        "Connection_errors_internal",
        "Connection_errors_max_connections",
        "Connection_errors_peer_address",
        "Connection_errors_select",
        "Connection_errors_tcpwrap",
        "Created_tmp_tables",
        "Created_tmp_disk_tables",
        "Sort_merge_passes",
        "Innodb_buffer_pool_reads",
        "Innodb_buffer_pool_read_requests",
        "Innodb_deadlocks",
    )

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def probe_runtime(self) -> ToolResult:
        started = utc_now()
        placeholders = ", ".join(["%s"] * len(self.STATUS_NAMES))
        sql = f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})"
        try:
            with closing(self.adapter.connect()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, self.STATUS_NAMES)
                    rows = list(cursor.fetchall())
            values = {row["Variable_name"]: _number(row["Value"]) for row in rows}
            return ToolResult.ok(
                {"status": values, "available_metrics": sorted(values)}, started, utc_now()
            )
        except Exception as exc:
            from .mysql import _error_result

            return _error_result(exc, started, utc_now())


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
