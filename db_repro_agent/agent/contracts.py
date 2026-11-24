"""Stable M10 contracts shared by the Chain and Post Agent entry points."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .action_generator import ActionPlan
from ..runtime.action_validator import ValidationResult


class ChainReproductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    incident_id: str = Field(min_length=1)
    target_node: str = Field(min_length=1)
    propagation_chain: list[str] = Field(min_length=1)
    database: str = Field(min_length=1)
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    indexes: dict[str, Any] = Field(default_factory=dict)
    data_scale: dict[str, Any] = Field(default_factory=dict)
    runtime_snapshot: dict[str, Any] = Field(default_factory=dict)
    workload_status: dict[str, Any] = Field(default_factory=dict)
    active_injection_nodes: list[str] = Field(default_factory=list)
    observed_downstream_nodes: list[str] = Field(default_factory=list)
    duration_budget_seconds: float = Field(gt=0)
    safety_constraints: dict[str, Any] = Field(default_factory=dict)
    previous_reflection: dict[str, Any] | None = None


class PostReproductionRequest(BaseModel):
    """M10 placeholder; Post parsing starts in M13."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    post: str = Field(min_length=1)


class ExperimentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    agent: Literal["chain", "post"]
    status: Literal["action_generated", "not_implemented", "rejected"]
    action_plan: ActionPlan | None = None
    validation: ValidationResult | None = None
    message: str | None = None
