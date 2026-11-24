"""Slow-query counter tracking relative to an explicit experiment marker."""

from __future__ import annotations

from dataclasses import dataclass

from ..tools.database.mysql import MySQLAdapter


@dataclass(frozen=True)
class SlowLogMarker:
    total_count: float


class SlowLogTracker:
    def __init__(self, adapter: MySQLAdapter) -> None:
        self.adapter = adapter

    def mark(self) -> SlowLogMarker:
        return SlowLogMarker(total_count=self._read_total())

    def collect(self, marker: SlowLogMarker) -> dict[str, float]:
        current = self._read_total()
        return {
            "slow_queries_total": current,
            "slow_queries_delta": max(0.0, current - marker.total_count),
        }

    def _read_total(self) -> float:
        result = self.adapter.execute_readonly("SHOW GLOBAL STATUS LIKE 'Slow_queries'")
        if not result.success:
            raise RuntimeError(result.error_message or "slow query counter query failed")
        if not result.data:
            return 0.0
        return float(result.data[0]["Value"])
