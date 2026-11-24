"""Pure deterministic Evidence Rule evaluation over a Metrics Timeline."""

from __future__ import annotations

import math
from statistics import mean
from typing import Iterable

from ..graph.evidence_registry import EvidenceRuleRegistry
from ..models.evidence import (
    EvidenceAggregation,
    EvidenceOperator,
    EvidenceReference,
    EvidenceResult,
    EvidenceRule,
    EvidenceSnapshotPair,
    EvidenceStatus,
)
from ..metrics.timeline import ExperimentPhase, ExperimentTimeline, MetricSample


class EvidenceEngine:
    def __init__(self, registry: EvidenceRuleRegistry) -> None:
        self.registry = registry

    def evaluate_rule(
        self,
        rule_id: str,
        timeline: ExperimentTimeline,
        *,
        phase: ExperimentPhase = ExperimentPhase.INJECTION,
        baseline_phase: ExperimentPhase = ExperimentPhase.BASELINE,
    ) -> EvidenceResult:
        return self.evaluate(self.registry.get(rule_id), timeline, phase=phase, baseline_phase=baseline_phase)

    def evaluate_snapshot_rule(self, rule_id: str, snapshots: EvidenceSnapshotPair) -> EvidenceResult:
        return self.evaluate_snapshot(self.registry.get(rule_id), snapshots)

    def evaluate_snapshot(self, rule: EvidenceRule, snapshots: EvidenceSnapshotPair) -> EvidenceResult:
        pre = _find_snapshot_metric(snapshots.pre_injection.metrics, rule.metric)
        post = _find_snapshot_metric(snapshots.post_action.metrics, rule.metric)
        if pre is None or post is None:
            return EvidenceResult(
                rule_id=rule.id,
                metric=rule.metric,
                status=EvidenceStatus.MISSING,
                hit=False,
                reason=f"snapshot metric {rule.metric} is missing",
            )
        if not math.isfinite(pre) or not math.isfinite(post):
            return EvidenceResult(
                rule_id=rule.id,
                metric=rule.metric,
                status=EvidenceStatus.INVALID,
                hit=False,
                reason="snapshot metric contains NaN or infinity",
            )
        comparison = post - pre
        hit = _compare(comparison, rule.operator, rule.threshold)
        return EvidenceResult(
            rule_id=rule.id,
            metric=rule.metric,
            status=EvidenceStatus.HIT if hit else EvidenceStatus.MISS,
            hit=hit,
            aggregate_value=post,
            reference_value=pre,
            comparison_value=comparison,
            samples_considered=1,
            longest_consecutive_samples=1 if hit else 0,
            reason="snapshot rule satisfied" if hit else "snapshot rule not satisfied",
        )

    def evaluate(
        self,
        rule: EvidenceRule,
        timeline: ExperimentTimeline,
        *,
        phase: ExperimentPhase = ExperimentPhase.INJECTION,
        baseline_phase: ExperimentPhase = ExperimentPhase.BASELINE,
    ) -> EvidenceResult:
        current = _metric_samples(timeline.samples, rule.metric, phase)
        if not current:
            return EvidenceResult(
                rule_id=rule.id,
                metric=rule.metric,
                status=EvidenceStatus.MISSING,
                hit=False,
                samples_considered=0,
                reason=f"metric {rule.metric} is missing in phase {phase.value}",
            )
        if any(not math.isfinite(sample.value) for sample in current):
            return EvidenceResult(
                rule_id=rule.id,
                metric=rule.metric,
                status=EvidenceStatus.INVALID,
                hit=False,
                samples_considered=len(current),
                reason="metric contains NaN or infinity",
            )

        baseline = _metric_samples(timeline.samples, rule.metric, baseline_phase)
        if any(not math.isfinite(sample.value) for sample in baseline):
            return EvidenceResult(
                rule_id=rule.id,
                metric=rule.metric,
                status=EvidenceStatus.INVALID,
                hit=False,
                samples_considered=len(current),
                reason="baseline contains NaN or infinity",
            )

        aggregate = _aggregate([sample.value for sample in current], rule.aggregation)
        reference_value: float | None = None
        comparison_value = aggregate
        if rule.reference == EvidenceReference.BASELINE:
            if not baseline:
                return _invalid(rule, len(current), "baseline metric is missing")
            reference_value = _aggregate([sample.value for sample in baseline], EvidenceAggregation.MEAN)
            if reference_value == 0:
                return _invalid(rule, len(current), "baseline value is zero; ratio or delta is undefined")
            if rule.aggregation == EvidenceAggregation.RATIO:
                comparison_value = aggregate / reference_value
            elif rule.aggregation == EvidenceAggregation.DELTA:
                comparison_value = aggregate - reference_value
        elif rule.reference == EvidenceReference.WINDOW_START:
            reference_value = current[0].value
            if rule.aggregation == EvidenceAggregation.DELTA:
                comparison_value = aggregate - reference_value

        hit, consecutive = _compare_with_consecutive(current, rule, reference_value, aggregate)
        return EvidenceResult(
            rule_id=rule.id,
            metric=rule.metric,
            status=EvidenceStatus.HIT if hit else EvidenceStatus.MISS,
            hit=hit,
            aggregate_value=aggregate,
            reference_value=reference_value,
            comparison_value=comparison_value,
            samples_considered=len(current),
            longest_consecutive_samples=consecutive,
            reason="evidence rule satisfied" if hit else "evidence rule not satisfied",
        )


def _metric_samples(samples: Iterable[MetricSample], metric: str, phase: ExperimentPhase) -> list[MetricSample]:
    return [
        sample
        for sample in samples
        if sample.phase == phase and (sample.metric_name == metric or sample.metric_name.endswith(f".{metric}"))
    ]


def _find_snapshot_metric(metrics: dict[str, float], metric: str) -> float | None:
    if metric in metrics:
        return metrics[metric]
    for name, value in metrics.items():
        if name.endswith(f".{metric}"):
            return value
    return None


def _aggregate(values: list[float], aggregation: EvidenceAggregation) -> float:
    if aggregation in {EvidenceAggregation.MEAN, EvidenceAggregation.RATIO, EvidenceAggregation.DELTA}:
        return mean(values)
    if aggregation == EvidenceAggregation.MAX:
        return max(values)
    if aggregation == EvidenceAggregation.MIN:
        return min(values)
    if aggregation == EvidenceAggregation.COUNT:
        return sum(values)
    if aggregation == EvidenceAggregation.P95:
        ordered = sorted(values)
        index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]
    raise ValueError(f"unsupported aggregation: {aggregation}")


def _compare_with_consecutive(
    samples: list[MetricSample],
    rule: EvidenceRule,
    reference_value: float | None,
    aggregate: float,
) -> tuple[bool, int]:
    current_run = 0
    longest = 0
    for sample in samples:
        comparison = sample.value
        if rule.aggregation == EvidenceAggregation.RATIO:
            comparison = sample.value / reference_value if reference_value not in (None, 0) else math.nan
        elif rule.aggregation == EvidenceAggregation.DELTA:
            comparison = sample.value - (reference_value or 0.0)
        satisfied = _compare(comparison, rule.operator, rule.threshold)
        current_run = current_run + 1 if satisfied else 0
        longest = max(longest, current_run)
    aggregate_hit = _compare(
        _comparison_value(rule, aggregate, reference_value), rule.operator, rule.threshold
    )
    return aggregate_hit and longest >= rule.min_consecutive_samples, longest


def _comparison_value(rule: EvidenceRule, aggregate: float, reference_value: float | None) -> float:
    if rule.aggregation == EvidenceAggregation.RATIO:
        return aggregate / reference_value if reference_value not in (None, 0) else math.nan
    if rule.aggregation == EvidenceAggregation.DELTA:
        if rule.reference == EvidenceReference.WINDOW_START:
            return aggregate - (reference_value or 0.0)
        return aggregate - (reference_value or 0.0)
    return aggregate


def _compare(value: float, operator: EvidenceOperator, threshold: float) -> bool:
    if not math.isfinite(value):
        return False
    if operator == EvidenceOperator.GT:
        return value > threshold
    if operator == EvidenceOperator.GTE:
        return value >= threshold
    if operator == EvidenceOperator.LT:
        return value < threshold
    if operator == EvidenceOperator.LTE:
        return value <= threshold
    return math.isclose(value, threshold, rel_tol=0.0, abs_tol=1e-12)


def _invalid(rule: EvidenceRule, sample_count: int, reason: str) -> EvidenceResult:
    return EvidenceResult(
        rule_id=rule.id,
        metric=rule.metric,
        status=EvidenceStatus.INVALID,
        hit=False,
        samples_considered=sample_count,
        reason=reason,
    )
