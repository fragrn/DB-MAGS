"""Executor for single or concurrent SQL actions."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from ..models.action import ActionResult, SQLAction
from ..tools.database.mysql import MySQLAdapter, MySQLConfig
from .command_executor import _iso


class SQLExecutor:
    def __init__(self, adapter: MySQLAdapter | None = None) -> None:
        self.adapter = adapter or MySQLAdapter()

    def validate(self, action: SQLAction) -> None:
        _validate_one_statement(action.sql)
        if action.execution_mode == "single" and action.concurrency != 1:
            raise ValueError("single execution_mode requires concurrency=1")

    def start(self, action: SQLAction) -> ActionResult:
        self.validate(action)
        now = _iso(time.time())
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="started",
            started_at=now,
            finished_at=now,
            duration_ms=0,
        )

    def execute(self, action: SQLAction) -> ActionResult:
        self.validate(action)
        started = time.time()
        if action.concurrency == 1:
            results = [self._execute_one(action)]
        else:
            with ThreadPoolExecutor(max_workers=action.concurrency) as pool:
                results = list(pool.map(lambda _: self._execute_one(action), range(action.concurrency)))
        failures = [result for result in results if not result.success]
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=not failures,
            status="succeeded" if not failures else failures[0].status,
            started_at=_iso(started),
            finished_at=_iso(time.time()),
            duration_ms=round((time.time() - started) * 1000, 3),
            error_code=failures[0].error_code if failures else None,
            error_message=failures[0].error_message if failures else None,
            data=[result.data for result in results],
            metadata={"concurrency": action.concurrency, "results": [result.model_dump() for result in results]},
        )

    def stop(self, action: SQLAction) -> ActionResult:
        return _lifecycle_result(action, "stopped")

    def cleanup(self, action: SQLAction) -> ActionResult:
        return _lifecycle_result(action, "cleaned")

    def _execute_one(self, action: SQLAction) -> ActionResult:
        started = time.time()
        try:
            config = self.adapter.config
            connection = self.adapter.connect(database=action.database)
            try:
                _set_socket_timeout(connection, action.timeout_sec)
                with connection.cursor() as cursor:
                    cursor.execute(action.sql)
                    data = list(cursor.fetchall()) if cursor.description else {"rowcount": cursor.rowcount}
                connection.commit()
            finally:
                connection.close()
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=True,
                status="succeeded",
                started_at=_iso(started),
                finished_at=_iso(time.time()),
                duration_ms=round((time.time() - started) * 1000, 3),
                data=data,
                metadata={"database": action.database},
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=False,
                status="failed",
                started_at=_iso(started),
                finished_at=_iso(time.time()),
                duration_ms=round((time.time() - started) * 1000, 3),
                error_code=exc.args[0] if getattr(exc, "args", None) else type(exc).__name__,
                error_message=str(exc),
                metadata={"database": action.database, "mysql_args": [str(x) for x in getattr(exc, "args", ())]},
            )


def _validate_one_statement(sql: str) -> None:
    if not sql.strip():
        raise ValueError("SQL must not be empty")
    if ";" in sql.rstrip().rstrip(";"):
        raise ValueError("multiple SQL statements are not allowed")


def _set_socket_timeout(connection, timeout_sec: float) -> None:
    """Apply the action timeout to a live PyMySQL socket when available."""

    sock = getattr(connection, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(timeout_sec)


def _lifecycle_result(action: SQLAction, status: str) -> ActionResult:
    now = _iso(time.time())
    return ActionResult(
        action_id=action.action_id,
        action_type=action.type,
        success=True,
        status=status,
        started_at=now,
        finished_at=now,
        duration_ms=0,
    )
