"""Deterministic Reflection strategy for fixed propagation chains."""

from __future__ import annotations

import math
from typing import Any

from ..models.reflection import ReflectionContext, ReflectionResult


class ChainReflector:
    """Diagnose propagation failures without changing the previous Action."""

    source = "chain"

    def reflect(self, context: ReflectionContext, *, attempt: int = 1) -> ReflectionResult:
        if context.source != self.source:
            raise ValueError("ChainReflector only accepts source='chain'")

        successful = _successful_action_ids(context.action_results)
        metrics = context.metrics_summary
        action = _last_action(context.previous_actions)

        if not _last_action_succeeded(context.action_results):
            return ReflectionResult(
                source="chain",
                attempt=attempt,
                diagnosis="ACTION_EXECUTION_FAILED: the generated Action did not execute successfully",
                recommended_changes={"regenerate_action": True},
                keep_successful_actions=successful,
                retryable=True,
                failed_node=context.failed_node,
                failed_edge=context.failed_edge,
            )

        if _number(metrics, "action_duration_sec") > _number(metrics, "injection_window_seconds") > 0:
            return _result(
                context,
                attempt,
                "ACTION_WINDOW_MISMATCH: action duration exceeds the injection window",
                {"duration_sec": max(1, int(_number(metrics, "injection_window_seconds")))},
                successful,
            )

        if _number(metrics, "terminals") > 0 and _number(metrics, "required_terminals") > _number(metrics, "terminals"):
            current = _number(metrics, "terminals")
            required = _number(metrics, "required_terminals")
            return _result(context, attempt, "WORKLOAD_TOO_LIGHT: workload terminals are below the required level", {"terminals": int(max(current + 1, required))}, successful)

        if _number(metrics, "concurrency") > 0 and _number(metrics, "required_concurrency") > _number(metrics, "concurrency"):
            current = _number(metrics, "concurrency")
            required = _number(metrics, "required_concurrency")
            return _result(context, attempt, "CONCURRENCY_TOO_LOW: action concurrency is below the observed requirement", {"concurrency": int(max(current + 1, required))}, successful)

        if context.failed_node in {"row_lock", "table_blocking", "metadata_lock_wait"} or "lock" in (context.failed_edge or ""):
            holder = _number(metrics, "holder_lock_seconds")
            wait_timeout = _number(metrics, "lock_wait_timeout_seconds")
            changes: dict[str, Any] = {}
            if holder > 0:
                changes["holder_lock_seconds"] = int(holder + 5)
            if wait_timeout > 0:
                changes["lock_wait_timeout_seconds"] = int(max(wait_timeout, holder + 10, 30))
            if changes:
                return _result(context, attempt, "LOCK_WINDOW_TOO_SHORT: the lock was not held long enough to establish the downstream wait", changes, successful)

        if context.failed_node == "slow_query" or "slow_query" in (context.failed_edge or ""):
            duration = _number(metrics, "query_duration_sec") or _number(action, "duration_sec")
            threshold = _number(metrics, "slow_query_threshold_sec")
            target = max(duration + 5, threshold if threshold else duration + 5)
            if context.remaining_budget > 0:
                target = min(target, context.remaining_budget)
            return _result(context, attempt, "SQL_DURATION_TOO_SHORT: the query did not run long enough to satisfy slow-query evidence", {"duration_sec": max(1, int(target))}, successful)

        if _number(metrics, "qps_ratio") >= 0.7 and (_number(metrics, "baseline_qps") > 0 or _number(metrics, "injection_qps") > 0):
            current_rate = _number(metrics, "rate")
            current_terminals = _number(metrics, "terminals")
            changes = {}
            if current_terminals > 0:
                changes["terminals"] = int(current_terminals + 1)
            if current_rate > 0:
                changes["rate"] = max(1, int(math.ceil(current_rate * 1.25)))
            return _result(context, attempt, "WORKLOAD_TOO_LIGHT: downstream throughput did not change enough", changes or {"regenerate_action": True}, successful)

        return _result(
            context,
            attempt,
            "EVIDENCE_MISS: Action executed but the expected propagation evidence was not observed",
            {"regenerate_action": True},
            successful,
        )


def _result(context: ReflectionContext, attempt: int, diagnosis: str, changes: dict[str, Any], successful: list[str]) -> ReflectionResult:
    return ReflectionResult(
        source="chain",
        attempt=attempt,
        diagnosis=diagnosis,
        recommended_changes=changes,
        keep_successful_actions=successful,
        retryable=True,
        failed_node=context.failed_node,
        failed_edge=context.failed_edge,
    )


def _last_action(actions: list[dict[str, Any]]) -> dict[str, Any]:
    return actions[-1] if actions else {}


def _last_action_succeeded(results: list[dict[str, Any]]) -> bool:
    return not results or bool(results[-1].get("success", False))


def _successful_action_ids(results: list[dict[str, Any]]) -> list[str]:
    return [str(item["action_id"]) for item in results if item.get("success") and item.get("action_id")]


def _number(values: dict[str, Any], key: str) -> float:
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0
