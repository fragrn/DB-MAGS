"""Common identifiers, statuses and error models used by every layer."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ResultStatus(StrEnum):
    WAITING_FOR_HUMAN = "waiting_for_human"
    SYSTEM_ERROR = "system_error"
    EXPERIMENT_MISS = "experiment_miss"
    EXPERIMENT_SUCCESS = "experiment_success"
    ABORTED = "aborted"


class ExperimentId(BaseModel):
    """Stable experiment identifier persisted with every artifact."""

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    @classmethod
    def new(cls, prefix: str = "exp") -> "ExperimentId":
        return cls(value=f"{prefix}_{uuid4().hex[:12]}")

    def __str__(self) -> str:
        return self.value


class ExperimentTimestamp(BaseModel):
    """UTC timestamp wrapper to prevent naive timestamps crossing module boundaries."""

    model_config = ConfigDict(frozen=True)

    value: datetime

    def __init__(self, **data: Any) -> None:
        value = data.get("value")
        if isinstance(value, str):
            data["value"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif value is None:
            data["value"] = datetime.now(timezone.utc)
        super().__init__(**data)
        if self.value.tzinfo is None:
            object.__setattr__(self, "value", self.value.replace(tzinfo=timezone.utc))
        else:
            object.__setattr__(self, "value", self.value.astimezone(timezone.utc))

    @classmethod
    def now(cls) -> "ExperimentTimestamp":
        return cls(value=datetime.now(timezone.utc))


class ErrorCode(StrEnum):
    CONFIGURATION = "configuration_error"
    ARTIFACT = "artifact_error"
    DATABASE = "database_error"
    INTERNAL = "internal_error"


class ReproductionError(Exception):
    """Base exception with a serializable error payload."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INTERNAL,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


def utc_now() -> datetime:
    """Return an aware UTC datetime for non-model callers."""

    return datetime.now(timezone.utc)
