"""Stable data structures for DBA-post input analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REQUIRED_OUTPUT_FIELDS = (
    "post_understanding",
    "experiment_environment",
    "background_workloads",
    "anomaly_injection",
    "expected_result",
    "open_questions",
)


@dataclass
class AnalysisRequest:
    """Natural-language DBA incident post and optional external metadata."""

    dba_description: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisRequest":
        description = str(data.get("dba_description") or data.get("description") or "").strip()
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object when provided")
        if not description:
            raise ValueError("dba_description is required")
        return cls(dba_description=description, metadata=metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dba_description": self.dba_description,
            "metadata": self.metadata,
        }


@dataclass
class ReproductionDesign:
    """LLM-produced design document for reproducing a database anomaly."""

    post_understanding: dict[str, Any]
    experiment_environment: dict[str, Any]
    background_workloads: list[dict[str, Any]]
    anomaly_injection: list[dict[str, Any]]
    expected_result: dict[str, Any]
    open_questions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReproductionDesign":
        missing = [field for field in REQUIRED_OUTPUT_FIELDS if field not in data]
        if missing:
            raise ValueError(f"LLM output missing required fields: {', '.join(missing)}")

        post_understanding = _require_dict(data, "post_understanding")
        experiment_environment = _require_dict(data, "experiment_environment")
        background_workloads = _require_list_of_dicts(data, "background_workloads")
        anomaly_injection = _require_list_of_dicts(data, "anomaly_injection")
        expected_result = _require_dict(data, "expected_result")
        open_questions = _require_string_list(data, "open_questions")

        return cls(
            post_understanding=post_understanding,
            experiment_environment=experiment_environment,
            background_workloads=background_workloads,
            anomaly_injection=anomaly_injection,
            expected_result=expected_result,
            open_questions=open_questions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_understanding": self.post_understanding,
            "experiment_environment": self.experiment_environment,
            "background_workloads": self.background_workloads,
            "anomaly_injection": self.anomaly_injection,
            "expected_result": self.expected_result,
            "open_questions": self.open_questions,
        }


def _require_dict(data: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = data.get(field_name)
    if not isinstance(value, dict):
        raise ValueError(f"LLM output field '{field_name}' must be an object")
    return value


def _require_list_of_dicts(data: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = data.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"LLM output field '{field_name}' must be an array of objects")
    return value


def _require_string_list(data: dict[str, Any], field_name: str) -> list[str]:
    value = data.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"LLM output field '{field_name}' must be an array of strings")
    return value
