"""Anomaly propagation graph models used by the synthetic M8 evaluator."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnomalyNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    evidence_rule_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnomalyEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    min_lag_sec: float = Field(default=0.0, ge=0.0)
    max_lag_sec: float | None = Field(default=None, ge=0.0)
    # A mapping from metric name to the minimum value required by the edge.
    mechanism_metrics: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_lag_window(self) -> "AnomalyEdge":
        if self.max_lag_sec is not None and self.max_lag_sec < self.min_lag_sec:
            raise ValueError("max_lag_sec must be greater than or equal to min_lag_sec")
        if self.source == self.target:
            raise ValueError("anomaly edge cannot point to itself")
        return self


class AnomalyGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: str = Field(min_length=1)
    nodes: list[AnomalyNode] = Field(min_length=1)
    edges: list[AnomalyEdge] = Field(default_factory=list)
    root_nodes: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
