"""Deterministic evidence rule and evaluation result models."""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceAggregation(StrEnum):
    MEAN = "mean"
    MAX = "max"
    MIN = "min"
    DELTA = "delta"
    RATIO = "ratio"
    COUNT = "count"
    P95 = "p95"


class EvidenceOperator(StrEnum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"


class EvidenceReference(StrEnum):
    ABSOLUTE = "absolute"
    BASELINE = "baseline"
    WINDOW_START = "window_start"
    SNAPSHOT = "snapshot"


class SnapshotBoundary(StrEnum):
    PRE_INJECTION = "pre_injection"
    POST_ACTION = "post_action"
    POST_RECOVERY = "post_recovery"


class EvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boundary: SnapshotBoundary
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metrics: dict[str, float] = Field(default_factory=dict)
    supporting_data: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    error: str | None = None


class EvidenceSnapshotPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pre_injection: EvidenceSnapshot
    post_action: EvidenceSnapshot


class EvidenceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    aggregation: EvidenceAggregation
    operator: EvidenceOperator
    threshold: float
    reference: EvidenceReference
    min_consecutive_samples: int = Field(default=1, ge=1)
    source: Literal["timeline", "snapshot"] = "timeline"


class EvidenceStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    MISSING = "missing"
    INVALID = "invalid"


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    metric: str
    status: EvidenceStatus
    hit: bool
    aggregate_value: float | None = None
    reference_value: float | None = None
    comparison_value: float | None = None
    samples_considered: int = 0
    longest_consecutive_samples: int = 0
    reason: str
