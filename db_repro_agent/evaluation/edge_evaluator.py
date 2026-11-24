"""Evaluate temporal and mechanism constraints for anomaly graph edges."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..models.anomaly_graph import AnomalyEdge
from .node_evaluator import NodeEvaluation


class EdgeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    satisfied: bool
    lag_sec: float | None = None
    temporal_valid: bool = False
    mechanism_valid: bool = True
    reason: str


class EdgeEvaluator:
    def evaluate(
        self,
        edge: AnomalyEdge,
        source: NodeEvaluation,
        target: NodeEvaluation,
    ) -> EdgeEvaluation:
        if not source.hit or not target.hit:
            return EdgeEvaluation(
                source=edge.source,
                target=edge.target,
                satisfied=False,
                lag_sec=_lag(source, target),
                temporal_valid=False,
                mechanism_valid=False,
                reason="source or target node did not hit",
            )
        lag = _lag(source, target)
        if lag is None:
            return EdgeEvaluation(
                source=edge.source,
                target=edge.target,
                satisfied=False,
                reason="source or target timestamp is missing",
            )
        temporal_valid = lag >= edge.min_lag_sec and (edge.max_lag_sec is None or lag <= edge.max_lag_sec)
        mechanism_valid = all(target.metrics.get(name, float("nan")) >= minimum for name, minimum in edge.mechanism_metrics.items())
        satisfied = temporal_valid and mechanism_valid
        if not temporal_valid:
            max_lag = "inf" if edge.max_lag_sec is None else f"{edge.max_lag_sec:g}"
            reason = f"lag {lag:g}s is outside [{edge.min_lag_sec:g}, {max_lag}]"
        elif not mechanism_valid:
            reason = "mechanism metric requirement is not satisfied"
        else:
            reason = "edge constraints satisfied"
        return EdgeEvaluation(
            source=edge.source,
            target=edge.target,
            satisfied=satisfied,
            lag_sec=lag,
            temporal_valid=temporal_valid,
            mechanism_valid=mechanism_valid,
            reason=reason,
        )


def _lag(source: NodeEvaluation, target: NodeEvaluation) -> float | None:
    if source.detected_at_sec is None or target.detected_at_sec is None:
        return None
    return target.detected_at_sec - source.detected_at_sec
