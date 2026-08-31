"""
Graph-driven evaluator for anomaly propagation experiments.

Evaluates at three layers:
  1. Node hit     — did each node in the target path show expected signals?
  2. Path hit     — did nodes hit in graph-consistent topological order?
  3. Overall      — weighted final score.
"""

from __future__ import annotations

from typing import Any

from agent.graph import ANOMALY_GRAPH
from agent.types import (
    EvaluationResult,
    EvidenceRule,
    NodeCategory,
    NodeResult,
    PathResult,
    ReActStep,
    TaskSpec,
)


# ---------------------------------------------------------------------------
# Metric comparison helpers
# ---------------------------------------------------------------------------

def _ratio(a: float, b: float) -> float:
    if b == 0:
        return float("inf") if a > 0 else 1.0
    return a / b


def _compare(
    metric: str,
    operator: str,
    threshold: float,
    baseline: dict[str, Any],
    after: dict[str, Any],
    metric_key: str,
) -> tuple[bool, float]:
    """
    Evaluate one EvidenceRule against baseline/after metric dicts.

    Returns (matched, confidence_delta).
    confidence_delta is 0.0 if not matched, or weight if matched.
    """
    bval = _extract_metric(baseline, metric_key)
    aval = _extract_metric(after, metric_key)

    # If we have no data at all, rule cannot be evaluated
    if bval is None and aval is None:
        return False, 0.0

    # Try to convert to float for numeric comparisons
    try:
        bval_f = float(bval) if bval is not None else None
    except (ValueError, TypeError):
        bval_f = None
    try:
        aval_f = float(aval) if aval is not None else None
    except (ValueError, TypeError):
        aval_f = None

    if operator == "ratio_up":
        matched = bool(aval_f is not None and bval_f is not None and aval_f > bval_f * threshold)
    elif operator == "ratio_down":
        matched = bool(aval_f is not None and bval_f is not None and bval_f > 0 and aval_f < bval_f * threshold)
    elif operator == ">":
        matched = bool(aval_f is not None and aval_f > threshold)
    elif operator == ">=":
        matched = bool(aval_f is not None and aval_f >= threshold)
    elif operator == "<":
        matched = bool(aval_f is not None and aval_f < threshold)
    elif operator == "<=":
        matched = bool(aval_f is not None and aval_f <= threshold)
    elif operator == "exists":
        matched = bool(aval is not None and aval != "")
    elif operator == "contains":
        matched = bool(str(aval or "").find(str(threshold)) >= 0)
    else:
        matched = False

    return matched, 1.0 if matched else 0.0


def _extract_metric(data: dict, metric: str) -> float | str | None:
    """
    Extract a metric value from a nested metrics dict.

    Supports dot-notation for nested keys (e.g. "db_metrics.Threads_running").
    """
    if metric in data:
        return data[metric]
    # Try dot notation
    parts = metric.split(".", 1)
    if len(parts) == 2:
        prefix, rest = parts
        if prefix in data and isinstance(data[prefix], dict):
            return data[prefix].get(rest)
    return None


# ---------------------------------------------------------------------------
# Single-node evaluation
# ---------------------------------------------------------------------------

def evaluate_node(
    node_id: str,
    baseline: dict[str, Any],
    after: dict[str, Any],
    execution_trace: dict[str, Any] | None = None,
    target_path: list[str] | None = None,
) -> NodeResult:
    """
    Evaluate whether a single anomaly node was hit.

    Iterates all evidence_rules.  A required rule that doesn't match
    immediately disqualifies the node.  Otherwise confidence is the
    sum of weights of matched rules divided by total possible weight.
    """
    if node_id == "slow_query":
        return _evaluate_slow_query(
            baseline,
            after,
            execution_trace or {},
            target_path or [],
        )

    node = ANOMALY_GRAPH.node(node_id)
    if node is None:
        return NodeResult(
            node_id=node_id,
            hit=False,
            confidence=0.0,
            details=f"Node '{node_id}' not found in graph",
        )

    if not node.evidence_rules:
        return NodeResult(
            node_id=node_id,
            hit=True,
            confidence=1.0,
            details="No evidence rules defined; node assumed hit",
        )

    total_weight = sum(r.weight for r in node.evidence_rules)
    matched_weight = 0.0
    evidence: dict[str, Any] = {}
    details_parts: list[str] = []

    for rule in node.evidence_rules:
        matched, conf = _compare(
            rule.metric, rule.operator, rule.threshold,
            baseline, after, rule.metric,
        )
        key = f"{rule.metric}__{rule.operator}__{rule.threshold}"
        evidence[key] = {
            "matched": matched,
            "confidence": conf,
            "rule_required": rule.required,
        }
        if matched:
            matched_weight += rule.weight
            details_parts.append(f"{rule.metric} {rule.operator} {rule.threshold} -> matched")
        else:
            details_parts.append(f"{rule.metric} {rule.operator} {rule.threshold} -> NOT matched")
            if rule.required:
                return NodeResult(
                    node_id=node_id,
                    hit=False,
                    confidence=0.0,
                    evidence=evidence,
                    details="Required rule not matched: " + "; ".join(details_parts),
                )

    confidence = matched_weight / total_weight if total_weight > 0 else 0.0
    hit = confidence >= 0.65

    return NodeResult(
        node_id=node_id,
        hit=hit,
        confidence=round(confidence, 3),
        evidence=evidence,
        details="; ".join(details_parts),
    )


def _evaluate_slow_query(
    baseline: dict[str, Any],
    after: dict[str, Any],
    execution_trace: dict[str, Any],
    target_path: list[str],
) -> NodeResult:
    raw_evidence = after.get("slow_log_evidence") or {}
    available = bool(raw_evidence.get("available"))
    target_entries = list(raw_evidence.get("target_entries") or [])
    target_entry_count = int(raw_evidence.get("target_entry_count") or len(target_entries))
    hit = available and target_entry_count >= 1
    start_variables = raw_evidence.get("variables_at_injection_start") or {}
    end_variables = raw_evidence.get("variables_at_injection_end") or {}
    reason_counts: dict[str, int] = {}
    for entry in target_entries:
        reason = str(entry.get("logging_reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    evidence = {
        "evidence_mode": "incremental_mysql_slow_log",
        "matched": hit,
        "available": available,
        "source": raw_evidence.get("source", "none"),
        "target_database": raw_evidence.get("target_database"),
        "new_entry_count": int(raw_evidence.get("entry_count") or 0),
        "target_entry_count": target_entry_count,
        "long_query_time_at_injection_start": start_variables.get("long_query_time"),
        "long_query_time_at_injection_end": end_variables.get("long_query_time"),
        "log_queries_not_using_indexes_at_injection_start": start_variables.get(
            "log_queries_not_using_indexes"
        ),
        "log_queries_not_using_indexes_at_injection_end": end_variables.get(
            "log_queries_not_using_indexes"
        ),
        "variables_at_injection_start": start_variables,
        "variables_at_injection_end": end_variables,
        "logging_reason_counts": reason_counts,
        "target_entries": target_entries,
        "error": raw_evidence.get("error") or raw_evidence.get("variable_error") or "",
    }
    if not available:
        details = "incremental MySQL slow-log evidence is unavailable"
    elif hit:
        details = (
            f"{target_entry_count} new slow-log entr{'y' if target_entry_count == 1 else 'ies'} "
            f"for database {raw_evidence.get('target_database')}"
        )
    else:
        details = f"no new slow-log entry for database {raw_evidence.get('target_database')}"
    return NodeResult(
        node_id="slow_query",
        hit=hit,
        confidence=1.0 if hit else 0.0,
        evidence=evidence,
        details=details,
    )


# ---------------------------------------------------------------------------
# Path evaluation
# ---------------------------------------------------------------------------

def evaluate_path(
    target_path: list[str],
    node_results: dict[str, NodeResult],
    execution_trace: dict[str, Any] | None = None,
) -> PathResult:
    """
    Evaluate whether a propagation chain was reproduced.

    Checks:
    - node_hit_ratio: fraction of path nodes that hit
    - ordered_hits: nodes that hit appear in topological order
    - path_hit: node_hit_ratio >= 0.75 AND terminal node hit
    """
    if not target_path:
        return PathResult(
            target_path=[],
            node_hit_ratio=0.0,
            ordered_hits=[],
            path_hit=False,
            failure_stage="empty_path",
            node_results=node_results,
        )

    hit_nodes = [n for n in target_path if node_results.get(n, NodeResult(n, False, 0.0)).hit]
    hit_ratio = len(hit_nodes) / len(target_path)

    # Check topological order
    ordered_hits: list[str] = []
    broken_edge: tuple[str, str] | None = None

    for i, node_id in enumerate(target_path):
        nr = node_results.get(node_id)
        if nr and nr.hit:
            # Ensure all predecessors in the path that should have hit already did
            preds = ANOMALY_GRAPH.predecessors(node_id)
            for pred in preds:
                if pred in target_path and pred not in ordered_hits:
                    pred_idx = target_path.index(pred)
                    if pred_idx < i:
                        # Predecessor should have already hit; if it didn't, note the broken edge
                        pred_nr = node_results.get(pred)
                        if pred_nr and not pred_nr.hit:
                            broken_edge = (pred, node_id)

            ordered_hits.append(node_id)

    terminal_hit = target_path[-1] in hit_nodes
    path_hit = hit_ratio >= 0.75 and terminal_hit

    failure_stage = ""
    if not terminal_hit:
        failure_stage = f"terminal_not_hit:{target_path[-1]}"
    elif broken_edge:
        failure_stage = f"broken_edge:{broken_edge[0]}->{broken_edge[1]}"
    elif hit_ratio < 0.75:
        failure_stage = f"low_node_hit_ratio:{round(hit_ratio,2)}"

    return PathResult(
        target_path=target_path,
        node_hit_ratio=round(hit_ratio, 3),
        ordered_hits=ordered_hits,
        broken_edge=broken_edge,
        path_hit=path_hit,
        failure_stage=failure_stage,
        node_results=node_results,
    )


# ---------------------------------------------------------------------------
# Full evaluation
# ---------------------------------------------------------------------------

def evaluate(
    baseline: dict[str, Any],
    after: dict[str, Any],
    target_path: list[str],
    execution_trace: dict[str, Any] | None = None,
) -> EvaluationResult:
    """
    Full evaluation combining node results, path results, and weighted scores.
    """
    # Evaluate each node in the target path
    node_results: dict[str, NodeResult] = {}
    for node_id in target_path:
        node_results[node_id] = evaluate_node(
            node_id,
            baseline,
            after,
            execution_trace,
            target_path=target_path,
        )

    path_result = evaluate_path(target_path, node_results, execution_trace)

    # Overall scores
    hit_count = sum(1 for nr in node_results.values() if nr.hit)
    target_anomaly_score = hit_count / len(target_path) if target_path else 0.0

    # Performance: check QPS drop / latency increase
    perf_score = _performance_score(baseline, after)

    # Causal order: did injectable nodes execute before their dependents?
    causal_score = _causal_order_score(execution_trace, target_path)

    # Stability: all tasks completed?
    stability_score = _stability_score(execution_trace)

    # Safety penalty (filled by runtime if violations occurred)
    safety_penalty = 0.0

    final_score = (
        0.30 * perf_score
        + 0.35 * target_anomaly_score
        + 0.25 * causal_score
        + 0.10 * stability_score
        - safety_penalty
    )

    success = final_score >= 0.70 and path_result.path_hit

    return EvaluationResult(
        performance_score=round(perf_score, 3),
        target_anomaly_score=round(target_anomaly_score, 3),
        causal_order_score=round(causal_score, 3),
        stability_score=round(stability_score, 3),
        safety_penalty=round(safety_penalty, 3),
        final_score=round(final_score, 3),
        success=success,
        reason=_build_reason(path_result, hit_count, len(target_path)),
        node_results={k: v.to_dict() for k, v in node_results.items()},
        path_result=path_result,
        baseline_metrics=baseline,
        after_metrics=after,
    )


def _performance_score(baseline: dict[str, Any], after: dict[str, Any]) -> float:
    """Detect QPS drop and latency increase."""
    wl_b = baseline.get("workload", {})
    wl_a = after.get("workload", {})
    qps_b = float(wl_b.get("qps", 1) or 1)
    qps_a = float(wl_a.get("qps", 0) or 0)
    latency_b = float(wl_b.get("avg_latency_ms", 1) or 1)
    latency_a = float(wl_a.get("avg_latency_ms", 0) or 0)

    if qps_a < qps_b * 0.8:
        return 1.0
    if latency_a > latency_b * 1.5:
        return 1.0
    # Partial score
    qps_ratio = qps_a / qps_b if qps_b > 0 else 0.0
    latency_ratio = latency_a / latency_b if latency_b > 0 else 1.0
    return min(1.0, (1.0 - qps_ratio + latency_ratio) / 2.0)


def _causal_order_score(
    trace: dict[str, Any] | None,
    target_path: list[str],
) -> float:
    """
    Check if injectable nodes appear before their dependents in the
    execution timeline.
    """
    if not trace or not target_path:
        return 1.0

    task_times: dict[str, float] = {}
    for task_id, result in trace.get("tasks", {}).items():
        if result.get("start_time"):
            try:
                import time
                task_times[task_id] = time.mktime(
                    time.strptime(result["start_time"], "%Y-%m-%dT%H:%M:%S")
                )
            except Exception:
                pass

    graph = ANOMALY_GRAPH
    ordered_count = 0
    total_edges = 0

    for i in range(len(target_path) - 1):
        src, dst = target_path[i], target_path[i + 1]
        total_edges += 1
        t_src = task_times.get(src)
        t_dst = task_times.get(dst)
        if t_src is not None and t_dst is not None and t_src <= t_dst:
            ordered_count += 1
        elif t_src is None or t_dst is None:
            # Cannot determine order; assume ok
            ordered_count += 1

    return ordered_count / total_edges if total_edges > 0 else 1.0


def _stability_score(trace: dict[str, Any] | None) -> float:
    """Check that all tasks completed without errors."""
    if not trace:
        return 1.0
    tasks = trace.get("tasks", {})
    if not tasks:
        return 1.0
    completed = sum(1 for t in tasks.values() if t.get("status") == "completed")
    return completed / len(tasks)


def _build_reason(path_result: PathResult, hit_count: int, total: int) -> str:
    if path_result.path_hit:
        return f"Propagation chain reproduced. {hit_count}/{total} nodes hit in topological order."
    return f"Propagation chain NOT fully reproduced. {hit_count}/{total} nodes hit. Failure at: {path_result.failure_stage}"


# ---------------------------------------------------------------------------
# Annotate for from __future__ import
# ---------------------------------------------------------------------------
from typing import Any
