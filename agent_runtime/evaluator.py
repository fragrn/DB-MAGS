from __future__ import annotations

from typing import Any, Dict

from agent_runtime.types import EvaluationResult, ExperimentRequest, TaskDAG


class Evaluator:
    def __init__(self, reward_threshold: float = 0.7):
        self.reward_threshold = reward_threshold

    def evaluate(
        self,
        request: ExperimentRequest,
        task_dag: TaskDAG,
        baseline_metrics: Dict[str, Any],
        after_metrics: Dict[str, Any],
        execution_trace: Dict[str, Any] | None = None,
    ) -> EvaluationResult:
        performance = self._performance_score(baseline_metrics, after_metrics)
        target_scores = self._target_scores(task_dag, baseline_metrics, after_metrics)
        causal_order = self._causal_order_score(request)
        stability = self._stability_score(after_metrics)
        safety_penalty = 1.0 if execution_trace and execution_trace.get("safety_violated") else 0.0
        final_score = max(0.0, min(1.0, performance * 0.4 + average(target_scores) * 0.3 + causal_order * 0.2 + stability * 0.1 - safety_penalty))
        chain_success = self._chain_success(request, target_scores, performance)
        success = chain_success if request.target_chain else final_score >= self.reward_threshold
        reason = "evaluation target reached" if success else "metrics did not meet reward or chain success threshold"
        return EvaluationResult(
            baseline_metrics=baseline_metrics,
            after_metrics=after_metrics,
            target_anomaly_scores=target_scores,
            reward={
                "performance_score": performance,
                "target_anomaly_score": average(target_scores),
                "causal_order_score": causal_order,
                "stability_score": stability,
                "safety_penalty": safety_penalty,
                "final_score": final_score,
                "success": success,
            },
            chain_events=self._chain_events(request, target_scores, performance),
            success=success,
            reason=reason,
        )

    @staticmethod
    def _performance_score(baseline: Dict[str, Any], after: Dict[str, Any]) -> float:
        baseline_qps = float(baseline.get("qps", 0.0) or 0.0)
        after_qps = float(after.get("qps", 0.0) or 0.0)
        baseline_p95 = float(baseline.get("p95_latency_ms", 0.0) or 0.0)
        after_p95 = float(after.get("p95_latency_ms", 0.0) or 0.0)
        qps_drop = max(0.0, (baseline_qps - after_qps) / baseline_qps) if baseline_qps else 0.0
        latency_rise = max(0.0, (after_p95 - baseline_p95) / baseline_p95) if baseline_p95 else 0.0
        return min(1.0, qps_drop * 2.0 + latency_rise)

    @staticmethod
    def _target_scores(task_dag: TaskDAG, baseline: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for node in task_dag.tasks.values():
            subtype = node.task_spec.anomaly_type
            if subtype in {"cpu", "io", "memory", "disk", "network"}:
                scores[subtype] = 1.0 if after.get("qps", 0) < baseline.get("qps", 0) else 0.4
            elif subtype in {"record_lock", "table_lock", "metadata_lock"}:
                scores[subtype] = 0.8 if after.get("p95_latency_ms", 0) >= baseline.get("p95_latency_ms", 0) else 0.4
            elif subtype == "database_table_backup":
                scores[subtype] = 0.7
            else:
                scores[subtype] = 0.8 if after.get("avg_latency_ms", 0) >= baseline.get("avg_latency_ms", 0) else 0.5
        return scores or {"overall": Evaluator._performance_score(baseline, after)}

    @staticmethod
    def _causal_order_score(request: ExperimentRequest) -> float:
        return 0.8 if request.target_chain else 0.6

    @staticmethod
    def _stability_score(after: Dict[str, Any]) -> float:
        return 0.6 if after.get("successful_transactions", 0) else 0.2

    @staticmethod
    def _chain_success(request: ExperimentRequest, target_scores: Dict[str, float], performance: float) -> bool:
        if not request.target_chain:
            return False
        return performance >= 0.5 and all(score >= 0.5 for score in target_scores.values())

    @staticmethod
    def _chain_events(request: ExperimentRequest, target_scores: Dict[str, float], performance: float) -> list[Dict[str, Any]]:
        events = []
        for index, node in enumerate(request.target_chain):
            metric = "performance" if index == len(request.target_chain) - 1 else node
            events.append({"node": node, "order": index, "score": target_scores.get(node, performance), "observed": target_scores.get(node, performance) >= 0.5})
        return events


def average(scores: Dict[str, float]) -> float:
    values = list(scores.values())
    return sum(values) / len(values) if values else 0.0
