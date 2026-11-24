"""Shared experiment lifecycle state model for Direct and Propagation runs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExperimentPhase(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    WORKLOAD_STARTING = "workload_starting"
    WARMUP = "warmup"
    BASELINE = "baseline"
    PLANNING = "planning"
    VALIDATING = "validating"
    INJECTING = "injecting"
    OBSERVING = "observing"
    RECOVERING = "recovering"
    EVALUATING = "evaluating"
    CLEANING_UP = "cleaning_up"
    SUCCESS = "success"
    FAILED = "failed"


class StateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_phase: ExperimentPhase
    to_phase: ExperimentPhase
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str | None = None


class ExperimentState(BaseModel):
    """Complete state snapshot; phase changes happen only through StateMachine."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str = Field(min_length=1)
    phase: ExperimentPhase = ExperimentPhase.CREATED
    attempt: int = Field(default=1, ge=1)
    mode: Literal["direct", "propagation"] | None = None
    action: Any | None = None
    execution_result: Any | None = None
    evaluation: Any | None = None
    step_results: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    history: list[StateTransition] = Field(default_factory=list)

    def with_step_result(self, phase: ExperimentPhase, result: Any) -> "ExperimentState":
        results = dict(self.step_results)
        results[phase.value] = result
        return self.model_copy(update={"step_results": results})
