"""Action validation and type-based dispatch."""

from __future__ import annotations

from typing import Any

from ..models.action import (
    ActionBase,
    ActionResult,
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
)
from ..tools.database.mysql import MySQLAdapter
from .benchbase_executor import BenchBaseExecutor
from .chaos_executor import ChaosExecutor
from .sql_executor import SQLExecutor
from .transaction_executor import TransactionExecutor


class ActionDispatcher:
    def __init__(self, adapter: MySQLAdapter | None = None) -> None:
        self.sql = SQLExecutor(adapter)
        self.transaction = TransactionExecutor(adapter)
        self.benchbase = BenchBaseExecutor()
        self.chaos = ChaosExecutor()

    def executor_for(self, action: ActionBase):
        if isinstance(action, SQLAction):
            return self.sql
        if isinstance(action, TransactionAction):
            return self.transaction
        if isinstance(action, BenchBaseAction):
            return self.benchbase
        if isinstance(action, ChaosBladeAction):
            return self.chaos
        raise TypeError(f"unsupported action type: {type(action).__name__}")

    def validate(self, action: ActionBase) -> None:
        self.executor_for(action).validate(action)

    def start(self, action: ActionBase) -> ActionResult:
        self.validate(action)
        return self.executor_for(action).start(action)

    def execute(self, action: ActionBase) -> ActionResult:
        self.validate(action)
        return self.executor_for(action).execute(action)

    def stop(self, action: ActionBase) -> ActionResult:
        return self.executor_for(action).stop(action)

    def cleanup(self, action: ActionBase) -> ActionResult:
        return self.executor_for(action).cleanup(action)

    def run(self, action: ActionBase) -> ActionResult:
        started = self.start(action)
        if not started.success:
            self.cleanup(action)
            return started
        try:
            return self.execute(action)
        finally:
            self.cleanup(action)
