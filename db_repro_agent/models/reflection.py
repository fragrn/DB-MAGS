"""Shared, business-neutral contracts for isolated Reflection strategies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..runtime.action_validator import ValidationResult


ReflectionSource = Literal["chain", "post"]


class ReflectionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ReflectionSource
    target: str = Field(min_length=1)
    previous_actions: list[dict[str, Any]] = Field(default_factory=list)
    action_results: list[dict[str, Any]] = Field(default_factory=list)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    node_results: dict[str, Any] = Field(default_factory=dict)
    edge_results: list[dict[str, Any]] = Field(default_factory=list)
    failed_node: str | None = None
    failed_edge: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    remaining_budget: float = Field(default=0.0, ge=0.0)


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ReflectionSource
    attempt: int = Field(ge=1)
    diagnosis: str = Field(min_length=1)
    recommended_changes: dict[str, Any] = Field(default_factory=dict)
    keep_successful_actions: list[str] = Field(default_factory=list)
    retryable: bool
    failed_node: str | None = None
    failed_edge: str | None = None


class AttemptOutcome(BaseModel):
    """Normalized output supplied by a real or fake attempt runner."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    action_result: dict[str, Any] = Field(default_factory=dict)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    node_results: dict[str, Any] = Field(default_factory=dict)
    edge_results: list[dict[str, Any]] = Field(default_factory=list)
    failed_node: str | None = None
    failed_edge: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)


class AttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    action_plan: Any
    validation: ValidationResult
    outcome: AttemptOutcome
    reflection: ReflectionResult | None = None


class ReflectionRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    attempts: list[AttemptRecord] = Field(default_factory=list)
    final_action_plan: Any | None = None
    final_failed_node: str | None = None
    final_failed_edge: str | None = None
    message: str | None = None
