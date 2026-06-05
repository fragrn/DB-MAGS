from __future__ import annotations

from typing import Any


TUNING_WHITELIST = {
    "inject_batch_size": [50000, 120000, 180000, 240000],
    "delete_ratio": [0.1, 0.25, 0.35],
    "query_runs": list(range(3, 11)),
    "sample_interval_sec": list(range(1, 6)),
    "concurrency": [10, 50, 100, 200],
    "lock_hold_seconds": [10, 30, 60],
    "work_mem_profile": ["512kB", "1MB", "2MB", "4MB"],
}
WORK_MEM_LOW_TO_HIGH = TUNING_WHITELIST["work_mem_profile"]


class ExperimentAgent:
    """Constrained strategy layer: choose chains and tune only whitelisted knobs."""

    def select_chain(self, goal: str | None, graph: dict[str, Any], explicit_chain: str | None) -> str:
        if explicit_chain:
            return explicit_chain
        goal_text = (goal or "").lower()
        if "dead" in goal_text or "stale" in goal_text or "spill" in goal_text:
            return "dead_tuples_to_temp_io"
        if "traffic" in goal_text or "resource" in goal_text:
            return "traffic_to_slow_query"
        if "lock" in goal_text or "long" in goal_text:
            return "long_tx_to_timeout"
        if "maintenance" in goal_text or "backup" in goal_text or "vacuum" in goal_text:
            return "maintenance_to_slow_query"
        if "slow" in goal_text:
            return "missing_index_to_timeout"
        return "dead_tuples_to_temp_io"

    def tune(self, failed_node: str | None, params: dict[str, Any]) -> dict[str, Any]:
        updated = dict(params)
        if failed_node == "dead_tuples":
            updated["inject_batch_size"] = self._next_value("inject_batch_size", updated.get("inject_batch_size"))
            updated["delete_ratio"] = self._next_value("delete_ratio", updated.get("delete_ratio"))
        elif failed_node == "stale_statistics":
            updated["inject_batch_size"] = self._next_value("inject_batch_size", updated.get("inject_batch_size"))
        elif failed_node == "poor_plan":
            updated["query_runs"] = self._next_value("query_runs", updated.get("query_runs"))
            updated["inject_batch_size"] = self._next_value("inject_batch_size", updated.get("inject_batch_size"))
        elif failed_node in {"sort_hash_spill", "temp_io_workfile_write"}:
            updated["query_runs"] = self._next_value("query_runs", updated.get("query_runs"))
            updated["work_mem_profile"] = self._lower_work_mem(updated.get("work_mem_profile", "1MB"))
        elif failed_node == "lock_contention":
            updated["lock_hold_seconds"] = self._next_value("lock_hold_seconds", updated.get("lock_hold_seconds"))
            updated["concurrency"] = self._next_value("concurrency", updated.get("concurrency"))
        elif failed_node == "slow_query":
            updated["concurrency"] = self._next_value("concurrency", updated.get("concurrency"))
            updated["query_runs"] = self._next_value("query_runs", updated.get("query_runs"))
        return updated

    def _next_value(self, key: str, current: Any) -> Any:
        allowed = TUNING_WHITELIST[key]
        if current not in allowed:
            return allowed[0]
        idx = allowed.index(current)
        return allowed[min(idx + 1, len(allowed) - 1)]

    def _lower_work_mem(self, current: Any) -> str:
        if current not in WORK_MEM_LOW_TO_HIGH:
            return "1MB"
        idx = WORK_MEM_LOW_TO_HIGH.index(current)
        return WORK_MEM_LOW_TO_HIGH[max(idx - 1, 0)]
