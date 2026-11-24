"""Incident representation and provenance-preserving merge rules."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field


class InformationSource(StrEnum):
    POST_FACT = "post_fact"
    AGENT_INFERENCE = "agent_inference"
    RUNTIME_OBSERVATION = "runtime_observation"
    HUMAN_INPUT = "human_input"


class EvidenceItem(BaseModel):
    """One fact or hypothesis with an explicit origin."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    value: Any
    source: InformationSource
    evidence: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class MissingInformation(BaseModel):
    """Information not available from the current IncidentSpec."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    critical: bool
    impact: str = Field(min_length=1)


class IncidentSpec(BaseModel):
    """Structured Incident language shared by later Agent and HITL modules."""

    model_config = ConfigDict(extra="forbid")

    incident_summary: str = Field(min_length=1)
    dbms: EvidenceItem | None = None
    version: EvidenceItem | None = None
    schema_facts: list[EvidenceItem] = Field(default_factory=list)
    data_facts: list[EvidenceItem] = Field(default_factory=list)
    sql: list[EvidenceItem] = Field(default_factory=list)
    configuration_facts: list[EvidenceItem] = Field(default_factory=list)
    workload_facts: list[EvidenceItem] = Field(default_factory=list)
    symptoms: list[EvidenceItem] = Field(default_factory=list)
    root_cause_hypotheses: list[EvidenceItem] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    provenance_conflicts: list[EvidenceItem] = Field(default_factory=list)


_EVIDENCE_FIELDS = (
    "schema_facts",
    "data_facts",
    "sql",
    "configuration_facts",
    "workload_facts",
    "symptoms",
    "root_cause_hypotheses",
)


def merge_evidence(
    existing: Iterable[EvidenceItem], additions: Iterable[EvidenceItem]
) -> list[EvidenceItem]:
    """Append new evidence without allowing one provenance source to overwrite another.

    Exact duplicate records are de-duplicated. Records with the same field but a
    different value, source, or evidence are intentionally retained as separate
    records so conflicts remain visible to later HITL and reasoning stages.
    """

    merged: list[EvidenceItem] = []
    seen: set[tuple[str, str, str, str | None, float | None]] = set()
    for item in [*existing, *additions]:
        key = (
            item.field,
            item.source.value,
            repr(item.value),
            item.evidence,
            item.confidence,
        )
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def merge_incident_specs(base: IncidentSpec, supplement: IncidentSpec) -> IncidentSpec:
    """Merge Incident information while preserving every source of truth.

    The base Incident remains first in every list. A supplement never silently
    replaces a post fact. Missing fields are considered resolved only when the
    supplement contains non-inferred evidence for that field.
    """

    values = base.model_dump()
    values["incident_summary"] = base.incident_summary or supplement.incident_summary

    conflicts: list[EvidenceItem] = list(base.provenance_conflicts)
    for scalar_field in ("dbms", "version"):
        base_item = getattr(base, scalar_field)
        supplement_item = getattr(supplement, scalar_field)
        values[scalar_field] = _merge_scalar_evidence(base_item, supplement_item)
        if base_item is not None and supplement_item is not None and base_item != supplement_item:
            conflicts = merge_evidence(conflicts, [base_item, supplement_item])

    resolved_fields: set[str] = set()
    for field_name in _EVIDENCE_FIELDS:
        merged = merge_evidence(getattr(base, field_name), getattr(supplement, field_name))
        values[field_name] = merged
        resolved_fields.update(
            item.field
            for item in getattr(supplement, field_name)
            if item.source != InformationSource.AGENT_INFERENCE
        )

    missing: list[MissingInformation] = []
    for item in [*base.missing_information, *supplement.missing_information]:
        if item.field not in resolved_fields and item not in missing:
            missing.append(item)
    values["missing_information"] = missing
    values["provenance_conflicts"] = conflicts
    return IncidentSpec.model_validate(values)


def _merge_scalar_evidence(
    base: EvidenceItem | None, supplement: EvidenceItem | None
) -> EvidenceItem | None:
    if base is None:
        return supplement
    if supplement is None:
        return base
    # A scalar field cannot hold two values without losing provenance. Promote
    # conflicting scalar evidence to the relevant facts list at the caller.
    # Keeping the original scalar is deliberate; callers can inspect the facts
    # for the complete conflict set.
    return base
