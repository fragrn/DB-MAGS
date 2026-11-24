"""Deterministic evaluator for a single Direct Incident oracle."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..metrics.timeline import ExperimentPhase, ExperimentTimeline
from ..models.common import ResultStatus
from ..models.evidence import EvidenceResult, EvidenceSnapshotPair
from ..graph.evidence_registry import EvidenceRuleRegistry
from .evidence_engine import EvidenceEngine


class IncidentEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    oracle_rule_id: str
    status: ResultStatus
    hit: bool
    evidence: EvidenceResult
    reason: str
    severity_evidence: EvidenceResult | None = None
    supporting_evidence: list[EvidenceResult] = Field(default_factory=list)
    degraded: bool = False


class IncidentEvaluator:
    """Map a deterministic EvidenceResult to a Direct reproduction outcome."""

    def __init__(self, registry: EvidenceRuleRegistry) -> None:
        self.engine = EvidenceEngine(registry)

    def evaluate(
        self,
        *,
        incident_id: str,
        oracle_rule_id: str,
        timeline: ExperimentTimeline,
        phase: ExperimentPhase = ExperimentPhase.OBSERVING,
        baseline_phase: ExperimentPhase = ExperimentPhase.BASELINE,
        snapshots: EvidenceSnapshotPair | None = None,
        severity_rule_id: str | None = None,
        supporting_rule_ids: list[str] | None = None,
    ) -> IncidentEvaluation:
        rule = self.engine.registry.get(oracle_rule_id)
        degraded = False
        if snapshots is not None and rule.source == "snapshot" and snapshots.pre_injection.success and snapshots.post_action.success:
            evidence = self.engine.evaluate_snapshot_rule(oracle_rule_id, snapshots)
        else:
            evidence = self.engine.evaluate_rule(oracle_rule_id, timeline, phase=phase, baseline_phase=baseline_phase)
            degraded = rule.source == "snapshot"
        hit = evidence.hit
        severity = None
        if severity_rule_id:
            severity_rule = self.engine.registry.get(severity_rule_id)
            if snapshots is not None and snapshots.pre_injection.success and snapshots.post_action.success:
                severity = self.engine.evaluate_snapshot_rule(severity_rule_id, snapshots)
            else:
                severity = self.engine.evaluate_rule(severity_rule_id, timeline, phase=phase, baseline_phase=baseline_phase)
                degraded = True
        supporting = []
        for rule_id in supporting_rule_ids or []:
            supporting.append(self.engine.evaluate_rule(rule_id, timeline, phase=phase, baseline_phase=baseline_phase))
        return IncidentEvaluation(
            incident_id=incident_id,
            oracle_rule_id=oracle_rule_id,
            status=ResultStatus.EXPERIMENT_SUCCESS if hit else ResultStatus.EXPERIMENT_MISS,
            hit=hit,
            evidence=evidence,
            reason="direct incident oracle hit" if hit else evidence.reason,
            severity_evidence=severity,
            supporting_evidence=supporting,
            degraded=degraded,
        )
