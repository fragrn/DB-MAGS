"""Concurrent transaction executor for holder/waiter experiments."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..models.action import ActionResult, TransactionAction, TransactionActor
from ..tools.database.mysql import MySQLAdapter
from .command_executor import _iso


class TransactionExecutor:
    def __init__(self, adapter: MySQLAdapter | None = None) -> None:
        self.adapter = adapter or MySQLAdapter()
        self._stop = threading.Event()

    def validate(self, action: TransactionAction) -> None:
        actor_ids = [actor.actor_id for actor in action.actors]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("transaction actor_id values must be unique")
        for actor in action.actors:
            for step in actor.steps:
                if ";" in step.sql.rstrip().rstrip(";"):
                    raise ValueError("each transaction step must contain one SQL statement")

    def start(self, action: TransactionAction) -> ActionResult:
        self.validate(action)
        self._stop.clear()
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

    def execute(self, action: TransactionAction) -> ActionResult:
        self.validate(action)
        started = time.time()
        with ThreadPoolExecutor(max_workers=len(action.actors)) as pool:
            futures = [pool.submit(self._run_actor, action, actor) for actor in action.actors]
            actor_results = [future.result() for future in futures]
        failures = [item for item in actor_results if not item["success"]]
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=not failures,
            status="succeeded" if not failures else "failed",
            started_at=_iso(started),
            finished_at=_iso(time.time()),
            duration_ms=round((time.time() - started) * 1000, 3),
            error_code=failures[0].get("error_code") if failures else None,
            error_message=failures[0].get("error_message") if failures else None,
            data=actor_results,
            metadata={"actor_count": len(action.actors)},
        )

    def stop(self, action: TransactionAction) -> ActionResult:
        self._stop.set()
        return _lifecycle_result(action, "stopped")

    def cleanup(self, action: TransactionAction) -> ActionResult:
        self._stop.set()
        return _lifecycle_result(action, "cleaned")

    def _run_actor(self, action: TransactionAction, actor: TransactionActor) -> dict:
        started = time.time()
        connection = None
        try:
            connection = self.adapter.connect(database=action.database)
            _set_socket_timeout(connection, action.timeout_sec)
            connection.autocommit(False)
            with connection.cursor() as cursor:
                for step in actor.steps:
                    if self._stop.is_set():
                        raise TimeoutError("transaction execution stopped")
                    cursor.execute(step.sql)
                    if step.delay_after_sec:
                        self._stop.wait(step.delay_after_sec)
            connection.commit()
            return {"actor_id": actor.actor_id, "role": actor.role, "success": True}
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return {
                "actor_id": actor.actor_id,
                "role": actor.role,
                "success": False,
                "error_code": exc.args[0] if getattr(exc, "args", None) else type(exc).__name__,
                "error_message": str(exc),
                "duration_ms": round((time.time() - started) * 1000, 3),
            }
        finally:
            if connection is not None:
                connection.close()


def _lifecycle_result(action: TransactionAction, status: str) -> ActionResult:
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


def _set_socket_timeout(connection, timeout_sec: float) -> None:
    sock = getattr(connection, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(timeout_sec)
