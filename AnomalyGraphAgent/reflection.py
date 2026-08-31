"""
Self-reflection module: analyzes a failed EvaluationResult and produces
a ReflectionResult with suggested parameter changes, memory updates, and
risk warnings.

Also provides an LLM-powered reflection path that attempts to infer
meaningful adjustments from the failure reason.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from agent.config import RuntimeConfig
from agent.graph import ANOMALY_GRAPH
from agent.types import EvaluationResult, ExperimentRequest, ReflectionResult


class ReflectionFallbackError(RuntimeError):
    """Raised when reflection would need to fall back to rule-based heuristics."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Rule-based suggestions by node type
# ---------------------------------------------------------------------------

_NODE_SUGGESTIONS: dict[str, dict[str, Any]] = {
    "traffic_surge": {
        "weak": "Adjust the generated raw benchbase_burst_command/raw_command: increase terminals, rate, or transaction mix without extending the observation window.",
        "strong": "Current traffic level is sufficient.",
    },
    "missing_index": {
        "weak": "Choose a larger table or a column with higher cardinality (more distinct values). "
                "Try a date range that covers more rows. "
                "Add ORDER BY on an unindexed column with LIMIT to force filesort.",
        "strong": "SQL pattern is well-aligned with missing_index cause.",
    },
    "improper_sql": {
        "weak": "Use SELECT * on a large table without WHERE. "
                "Try a JOIN with no index on the join column.",
        "strong": "SQL shape is generating sufficient scan.",
    },
    "long_tx": {
        "weak": "Increase hold_sec to give more time for contention to build. "
                "Ensure the updated row is also accessed by other sessions.",
        "strong": "Transaction holding time is adequate.",
    },
    "hot_update": {
        "weak": "Increase waiter_concurrency. "
                "Ensure holder and waiters target the same primary key value. "
                "Reduce sleep between waiter retries.",
        "strong": "Lock conflict pattern is generating sufficient contention.",
    },
    "lock_contention": {
        "weak": "Prior nodes need to produce more concurrent access before this node. "
                "Increase traffic_surge concurrency or ensure holder/waiter concurrency is high enough.",
        "strong": "Lock contention is building as expected.",
    },
    "poor_plan": {
        "weak": "Run ANALYZE TABLE to clear stale stats, then retry. "
                "Choose a column with lower selectivity for the filter.",
        "strong": "Execution plan is as expected.",
    },
    "slow_query": {
        "weak": "The upstream nodes are not producing sufficient query pressure. "
                "Increase concurrency or duration of prior tasks.",
        "strong": "Slow query pattern is confirmed.",
    },
    "resource_cpu": {
        "weak": "Adjust the generated ChaosBlade raw_command CPU stress intensity or duration. "
                "Ensure the database is under active query load.",
        "strong": "ChaosBlade CPU stress is being applied.",
    },
    "resource_io": {
        "weak": "Adjust the generated ChaosBlade raw_command IO stress duration or disk parameters.",
        "strong": "ChaosBlade IO stress is adequate.",
    },
    "backup": {
        "weak": "Run mysqldump on the largest table. "
                "Ensure the table is actively accessed during the backup.",
        "strong": "Backup is running as expected.",
    },
}


# ---------------------------------------------------------------------------
# Rule-based parameter adjustments
# ---------------------------------------------------------------------------

_NODE_PARAM_UPDATES: dict[str, dict[str, Any]] = {
    "missing_index": {
        "table": "use_larger_table",
        "limit": "reduce_to_50",
        "predicate": "widen_range",
        "pattern": "sort_filesort_if_no_range",
    },
    "improper_sql": {
        "pattern": "large_scan",
        "limit": "increase_to_500",
    },
    "hot_update": {
        "waiter_concurrency": "increase_by_4",
        "hold_sec": "increase_by_10",
    },
    "lock_contention": {
        "holder_concurrency": "increase_by_1",
        "waiter_concurrency": "increase_by_4",
    },
}


# ---------------------------------------------------------------------------
# SelfReflection
# ---------------------------------------------------------------------------

class SelfReflection:
    """Analyzes evaluation failures and produces actionable feedback."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        memory_items: Optional[List[dict]] = None,
    ) -> ReflectionResult:
        """
        Generate a ReflectionResult from a failed EvaluationResult.

        Tries LLM reflection and fails hard if rule-based fallback would be needed.
        """
        if not self.config.planner_enabled:
            raise ReflectionFallbackError("Reflection fallback blocked: planner_enabled=false")
        if not self.config.openai_api_key:
            raise ReflectionFallbackError("Reflection fallback blocked: missing OPENAI_API_KEY")
        try:
            llm_result = self._llm_reflect(evaluation, request, memory_items or [])
        except Exception as exc:
            if exc.__class__.__name__ == "LLMTimeoutError":
                raise ReflectionFallbackError(f"Reflection LLM timeout: {exc}") from exc
            raise
        if llm_result and llm_result.failure_reason:
            return llm_result
        raise ReflectionFallbackError("Reflection fallback blocked: LLM reflection failed or returned empty failure_reason")

    # -------------------------------------------------------------------------
    # LLM-powered reflection
    # -------------------------------------------------------------------------

    def _llm_reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        memory_items: list[dict],
    ) -> ReflectionResult | None:
        """Attempt LLM-based reflection if the LLM is available."""
        prompt = self._build_reflection_prompt(evaluation, request, memory_items)

        try:
            from agent import tools as tool_registry
            response = tool_registry.llm_generate(
                config=self.config,
                system_prompt="You are a database chaos engineering expert. "
                              "Analyze failed anomaly propagation experiments and suggest precise parameter adjustments.",
                user_prompt=prompt,
                temperature=0.3,
                json_mode=True,
            )
            if response.get("error"):
                return None

            payload = response.get("json_payload")
            if not payload:
                text = response.get("text", "")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    return None

            failure_reason = payload.get("failure_reason", "")
            suggestions = payload.get("suggested_changes", [])
            param_updates = payload.get("task_parameter_updates", {})
            risk_warning = payload.get("risk_warning", "")

            return ReflectionResult(
                failure_reason=failure_reason,
                suggested_changes=suggestions if isinstance(suggestions, list) else [],
                task_parameter_updates=param_updates if isinstance(param_updates, dict) else {},
                risk_warning=risk_warning,
                memory_update=self._build_memory_update(evaluation, request),
                raw_text=response.get("text", ""),
            )
        except tool_registry.LLMTimeoutError:
            raise
        except Exception:
            return None

    def _build_reflection_prompt(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        memory_items: list[dict],
    ) -> str:
        """Build the LLM reflection prompt."""
        path_result = evaluation.path_result
        target_path = path_result.target_path if path_result else []
        failed_nodes = evaluation.failed_nodes
        broken_edge = path_result.broken_edge if path_result else None

        memory_summary = ""
        if memory_items:
            memory_summary = "Prior memory:\n" + "\n".join(
                f"- anomaly={m.get('anomaly')}, outcome={m.get('outcome')}, "
                f"success={m.get('success')}, node_hit_ratio={m.get('node_hit_ratio')}"
                for m in memory_items[:5]
            )

        node_results_summary = ""
        if evaluation.node_results:
            for nid, nr in evaluation.node_results.items():
                if hasattr(nr, "to_dict"):
                    nr_dict = nr.to_dict()
                else:
                    nr_dict = nr
                node_results_summary += (
                    f"  {nid}: hit={nr_dict.get('hit')}, "
                    f"confidence={nr_dict.get('confidence')}, "
                    f"details={nr_dict.get('details', '')[:100]}\n"
                )

        duration_limit = float(request.max_duration_sec)
        workload = request.workload or {}
        if workload.get("enabled"):
            observe_sec = float(workload.get("injection_observe_sec", duration_limit) or duration_limit)
            duration_limit = min(duration_limit, observe_sec)

        return f"""## Failed Experiment
target_anomaly: {request.target_anomaly}
target_database: {request.target_database}
target_path: {' -> '.join(target_path)}

## Evaluation Results
final_score: {evaluation.final_score:.3f}
success: {evaluation.success}
performance_score: {evaluation.performance_score:.3f}
target_anomaly_score: {evaluation.target_anomaly_score:.3f}
causal_order_score: {evaluation.causal_order_score:.3f}
stability_score: {evaluation.stability_score:.3f}
reason: {evaluation.reason}

## Node Results
{node_results_summary if node_results_summary else 'N/A'}

## Failed Nodes
{failed_nodes if failed_nodes else 'None'}

## Broken Edge (if propagation was interrupted)
{broken_edge if broken_edge else 'None'}

## Prior Memory
{memory_summary if memory_summary else 'No prior memory.'}

## Your Task
Analyze why the propagation chain was not reproduced. Suggest concrete parameter
changes for each failed or weak node. Return a JSON object with:
- failure_reason: string explaining the most likely root cause of failure
- suggested_changes: array of strings with specific suggestions
- task_parameter_updates: object mapping node_id -> parameter -> new_value
- risk_warning: any safety concern about the suggested changes

Constraints:
- target_path and injected_nodes are user-owned; do not change them.
- task_parameter_updates may only contain user-specified injected_nodes.
- Suggest concrete raw action TaskSpec changes: SQL text, transaction script steps, command argv, concurrency, duration_sec, table, column, predicate, terminals, rate, or transaction mix.
- duration_sec is a hard limit: every suggested action duration must be positive and <= {duration_limit:g} seconds.
- Do not suggest extending the observation window. Prefer concurrency, terminals, rate, resource intensity, hot-key concentration, or transaction mix changes.
- For resource_cpu/resource_io/resource_memory/resource_network, command argv changes must stay within ChaosBlade raw_command semantics.
- Do not suggest adding lock_holder, slow_sql, qps_drop, or any non-injected task.
- If the user-selected injected_nodes are insufficient, say the user needs to add injected_nodes in failure_reason or suggested_changes, but do not add them yourself.
"""

    # -------------------------------------------------------------------------
    # Rule-based reflection
    # -------------------------------------------------------------------------

    def _rule_based_reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
    ) -> ReflectionResult:
        """Rule-based fallback when LLM is unavailable."""
        failure_reason = evaluation.reason
        suggested_changes: list[str] = []
        task_parameter_updates: dict[str, dict] = {}
        risk_warning = ""

        path_result = evaluation.path_result
        target_path = path_result.target_path if path_result else []

        # Analyze each node result
        if evaluation.node_results:
            for node_id, nr in evaluation.node_results.items():
                if hasattr(nr, "hit"):
                    hit = nr.hit
                    confidence = nr.confidence if hasattr(nr, "confidence") else 0.0
                else:
                    hit = nr.get("hit", False)
                    confidence = nr.get("confidence", 0.0)

                if not hit and node_id in _NODE_SUGGESTIONS:
                    hints = _NODE_SUGGESTIONS[node_id]
                    if confidence < 0.5:
                        suggested_changes.append(hints["weak"])
                    if node_id in _NODE_PARAM_UPDATES:
                        task_parameter_updates[node_id] = _NODE_PARAM_UPDATES[node_id]

        # Check for broken edges
        if path_result and path_result.broken_edge:
            src, dst = path_result.broken_edge
            suggested_changes.append(
                f"Edge {src} -> {dst} did not propagate. "
                f"Increase {src} intensity or add an intermediate task."
            )

        # Check for low causal order score
        if evaluation.causal_order_score < 0.5:
            suggested_changes.append(
                "Causal ordering is poor. Ensure injectable tasks start before "
                "their dependent tasks in the DAG."
            )

        # Safety check
        if evaluation.safety_violations:
            risk_warning = (
                f"Safety violations detected: {', '.join(evaluation.safety_violations)}. "
                "Review and adjust task parameters before retry."
            )

        return ReflectionResult(
            failure_reason=failure_reason,
            suggested_changes=suggested_changes,
            task_parameter_updates=task_parameter_updates,
            risk_warning=risk_warning,
            memory_update=self._build_memory_update(evaluation, request),
        )

    def _build_memory_update(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
    ) -> list[dict]:
        """Build memory_update entries from the evaluation."""
        path_result = evaluation.path_result
        target_path = path_result.target_path if path_result else []
        node_hit_ratio = path_result.node_hit_ratio if path_result else 0.0

        return [
            {
                "anomaly": request.target_anomaly,
                "path": target_path,
                "outcome": evaluation.reason,
                "success": evaluation.success,
                "round": 0,  # filled by caller
                "node_hit_ratio": node_hit_ratio,
            }
        ]
