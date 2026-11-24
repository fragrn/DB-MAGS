"""MySQL runtime counters converted into per-second metrics."""

from __future__ import annotations

import time
from typing import Any

from ..tools.database.mysql import MySQLAdapter


class MySQLMetricsProvider:
    STATUS_NAMES = (
        "Threads_connected",
        "Threads_running",
        "Questions",
        "Com_commit",
        "Com_rollback",
        "Connections",
        "Connection_errors_accept",
        "Connection_errors_internal",
        "Connection_errors_max_connections",
        "Connection_errors_peer_address",
        "Connection_errors_select",
        "Connection_errors_tcpwrap",
        "Aborted_clients",
        # Current waits is retained as a diagnostic gauge. Evidence uses the
        # cumulative wait counter below because a short-lived wait can be
        # missed by periodic sampling.
        "Innodb_row_lock_current_waits",
        "Innodb_row_lock_waits",
        "Innodb_row_lock_time",
        "Slow_queries",
        "Created_tmp_tables",
        "Created_tmp_disk_tables",
        "Sort_merge_passes",
        "Innodb_buffer_pool_reads",
        "Innodb_buffer_pool_read_requests",
        "Innodb_deadlocks",
        "Innodb_log_waits",
        "Innodb_os_log_pending_fsyncs",
        "Innodb_os_log_pending_writes",
        "Max_used_connections",
        "Aborted_connects",
    )

    def __init__(self, adapter: MySQLAdapter) -> None:
        self.adapter = adapter
        self._previous: dict[str, float] | None = None
        self._previous_at: float | None = None

    def collect(self) -> dict[str, float]:
        counters = self._read_status()
        now = time.monotonic()
        metrics = {
            "threads_connected": counters.get("Threads_connected", 0.0),
            "threads_running": counters.get("Threads_running", 0.0),
            "row_lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "row_lock_current_waits": counters.get("Innodb_row_lock_current_waits", 0.0),
            "row_lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "lock_waits_current": counters.get("Innodb_row_lock_current_waits", 0.0),
            "lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "slow_queries_total": counters.get("Slow_queries", 0.0),
            "innodb_log_waits": counters.get("Innodb_log_waits", 0.0),
            "innodb_os_log_pending_fsyncs": counters.get("Innodb_os_log_pending_fsyncs", 0.0),
            "innodb_os_log_pending_writes": counters.get("Innodb_os_log_pending_writes", 0.0),
            "max_used_connections": counters.get("Max_used_connections", 0.0),
            "aborted_connects": counters.get("Aborted_connects", 0.0),
            "aborted_clients": counters.get("Aborted_clients", 0.0),
            "connections_total": counters.get("Connections", 0.0),
            "created_tmp_tables": counters.get("Created_tmp_tables", 0.0),
            "created_tmp_disk_tables": counters.get("Created_tmp_disk_tables", 0.0),
            "sort_merge_passes": counters.get("Sort_merge_passes", 0.0),
            "innodb_buffer_pool_reads": counters.get("Innodb_buffer_pool_reads", 0.0),
            "innodb_buffer_pool_read_requests": counters.get("Innodb_buffer_pool_read_requests", 0.0),
            "innodb_deadlocks": counters.get("Innodb_deadlocks", 0.0),
        }
        lock_metrics = self._lock_metrics()
        metrics.update(lock_metrics)
        connection_errors = _connection_errors(counters)
        metrics["connection_errors_total"] = connection_errors
        if self._previous is not None and self._previous_at is not None:
            elapsed = max(now - self._previous_at, 1e-9)
            metrics["qps"] = max(0.0, counters.get("Questions", 0.0) - self._previous.get("Questions", 0.0)) / elapsed
            commits = counters.get("Com_commit", 0.0) + counters.get("Com_rollback", 0.0)
            previous_commits = self._previous.get("Com_commit", 0.0) + self._previous.get("Com_rollback", 0.0)
            metrics["tps"] = max(0.0, commits - previous_commits) / elapsed
            metrics["innodb_log_waits_delta"] = max(0.0, counters.get("Innodb_log_waits", 0.0) - self._previous.get("Innodb_log_waits", 0.0))
            metrics["aborted_connects_delta"] = max(0.0, counters.get("Aborted_connects", 0.0) - self._previous.get("Aborted_connects", 0.0))
            metrics["connection_error_delta"] = max(0.0, connection_errors - _connection_errors(self._previous))
            metrics["created_tmp_tables_delta"] = max(0.0, counters.get("Created_tmp_tables", 0.0) - self._previous.get("Created_tmp_tables", 0.0))
            metrics["created_tmp_disk_tables_delta"] = max(0.0, counters.get("Created_tmp_disk_tables", 0.0) - self._previous.get("Created_tmp_disk_tables", 0.0))
            metrics["sort_merge_passes_delta"] = max(0.0, counters.get("Sort_merge_passes", 0.0) - self._previous.get("Sort_merge_passes", 0.0))
            metrics["innodb_buffer_pool_reads_delta"] = max(0.0, counters.get("Innodb_buffer_pool_reads", 0.0) - self._previous.get("Innodb_buffer_pool_reads", 0.0))
            metrics["deadlock_delta"] = max(0.0, counters.get("Innodb_deadlocks", 0.0) - self._previous.get("Innodb_deadlocks", 0.0))
            metrics["row_lock_waits_delta"] = max(0.0, counters.get("Innodb_row_lock_waits", 0.0) - self._previous.get("Innodb_row_lock_waits", 0.0))
            metrics["row_lock_wait_time_ms_delta"] = max(0.0, counters.get("Innodb_row_lock_time", 0.0) - self._previous.get("Innodb_row_lock_time", 0.0))
        else:
            metrics["qps"] = 0.0
            metrics["tps"] = 0.0
            metrics["innodb_log_waits_delta"] = 0.0
            metrics["aborted_connects_delta"] = 0.0
            metrics["connection_error_delta"] = 0.0
            metrics["created_tmp_tables_delta"] = 0.0
            metrics["created_tmp_disk_tables_delta"] = 0.0
            metrics["sort_merge_passes_delta"] = 0.0
            metrics["innodb_buffer_pool_reads_delta"] = 0.0
            metrics["deadlock_delta"] = 0.0
            metrics["row_lock_waits_delta"] = 0.0
            metrics["row_lock_wait_time_ms_delta"] = 0.0
        self._previous = counters
        self._previous_at = now
        return metrics

    def snapshot(self) -> dict[str, float]:
        counters = self._read_status()
        return {
            "threads_connected": counters.get("Threads_connected", 0.0),
            "threads_running": counters.get("Threads_running", 0.0),
            "row_lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "row_lock_current_waits": counters.get("Innodb_row_lock_current_waits", 0.0),
            "row_lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "lock_waits_current": counters.get("Innodb_row_lock_current_waits", 0.0),
            "lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "slow_queries_total": counters.get("Slow_queries", 0.0),
            "questions_total": counters.get("Questions", 0.0),
            "commits_total": counters.get("Com_commit", 0.0) + counters.get("Com_rollback", 0.0),
            "connections_total": counters.get("Connections", 0.0),
            "connection_errors_total": _connection_errors(counters),
            "created_tmp_tables": counters.get("Created_tmp_tables", 0.0),
            "created_tmp_disk_tables": counters.get("Created_tmp_disk_tables", 0.0),
            "sort_merge_passes": counters.get("Sort_merge_passes", 0.0),
            "innodb_buffer_pool_reads": counters.get("Innodb_buffer_pool_reads", 0.0),
            "innodb_buffer_pool_read_requests": counters.get("Innodb_buffer_pool_read_requests", 0.0),
            "innodb_deadlocks": counters.get("Innodb_deadlocks", 0.0),
            **self._lock_metrics(),
        }

    def _lock_metrics(self) -> dict[str, float]:
        metadata_result = self.adapter.execute_readonly(
            "SELECT COUNT(*) AS wait_count FROM performance_schema.metadata_locks "
            "WHERE LOCK_STATUS = 'PENDING'"
        )
        table_result = self.adapter.execute_readonly(
            "SELECT COUNT(*) AS wait_count FROM performance_schema.metadata_locks pending "
            "WHERE pending.LOCK_STATUS = 'PENDING' "
            "AND pending.OBJECT_TYPE = 'TABLE' "
            "AND EXISTS ("
            "SELECT 1 FROM performance_schema.metadata_locks granted "
            "WHERE granted.LOCK_STATUS = 'GRANTED' "
            "AND granted.OBJECT_TYPE = 'TABLE' "
            "AND granted.OBJECT_SCHEMA = pending.OBJECT_SCHEMA "
            "AND granted.OBJECT_NAME = pending.OBJECT_NAME "
            "AND granted.OWNER_THREAD_ID <> pending.OWNER_THREAD_ID"
            ")"
        )
        current_table_result = self.adapter.execute_readonly(
            "SELECT COUNT(*) AS wait_count FROM performance_schema.metadata_locks "
            "WHERE LOCK_STATUS = 'PENDING' AND OBJECT_TYPE = 'TABLE'"
        )
        metrics = {
            "metadata_lock_wait_count": _first_count(metadata_result),
            "table_blocking_wait_count": _first_count(table_result),
            "table_blocking_current_count": _first_count(current_table_result),
            "table_blocking_probe_degraded": float(
                not (metadata_result.success and table_result.success and current_table_result.success)
            ),
        }
        return metrics

    def snapshot_supporting(self) -> dict[str, Any]:
        result = self.adapter.execute_readonly(
            "SELECT * FROM performance_schema.data_lock_waits"
        )
        locks = self._lock_metrics()
        result_data: dict[str, Any] = {
            "available": result.success,
            "data_lock_waits": result.data if result.success else [],
            "lock_metrics": locks,
        }
        if not result.success:
            result_data["error"] = result.error_message
        return result_data

    def _read_status(self) -> dict[str, float]:
        placeholders = ", ".join(["%s"] * len(self.STATUS_NAMES))
        result = self.adapter.execute_readonly(
            f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})", self.STATUS_NAMES
        )
        if not result.success:
            raise RuntimeError(result.error_message or "MySQL metrics query failed")
        return {row["Variable_name"]: float(row["Value"]) for row in result.data}


def _connection_errors(counters: dict[str, float]) -> float:
    return sum(value for name, value in counters.items() if name.startswith("Connection_errors_"))


def _first_count(result) -> float:
    if not result.success or not result.data:
        return 0.0
    try:
        return float(result.data[0].get("wait_count", 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0
