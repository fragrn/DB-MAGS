"""Executable action contracts and unified executor results."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionResultStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    CLEANED = "cleaned"


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    action_type: str
    success: bool
    status: ActionResultStatus
    started_at: str
    finished_at: str
    duration_ms: float
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    data: object | None = None
    error_code: int | str | None = None
    error_message: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    target_node: str = Field(min_length=1)


class SQLAction(ActionBase):
    type: Literal["sql"] = "sql"
    database: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1, le=1024)
    duration_sec: int = Field(default=1, ge=1, le=86400)
    timeout_sec: float = Field(default=30.0, gt=0, le=86400)
    execution_mode: Literal["single", "concurrent"] = "single"


class TransactionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1)
    delay_after_sec: float = Field(default=0.0, ge=0, le=86400)


class TransactionActor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1)
    role: Literal["holder", "waiter", "worker"] = "worker"
    steps: list[TransactionStep] = Field(min_length=1)


class TransactionAction(ActionBase):
    type: Literal["transaction"] = "transaction"
    database: str = Field(min_length=1)
    actors: list[TransactionActor] = Field(min_length=1)
    duration_sec: int = Field(default=30, ge=1, le=86400)
    timeout_sec: float = Field(default=60.0, gt=0, le=86400)


class BenchBaseAction(ActionBase):
    type: Literal["benchbase"] = "benchbase"
    benchmark: str = Field(min_length=1)
    database: str = Field(min_length=1)
    terminals: int = Field(ge=1, le=100000)
    rate: int | None = Field(
        default=None,
        ge=1,
        description="Optional request rate; omit when unlimited, otherwise at least 1.",
    )
    duration_sec: int = Field(ge=1, le=86400)
    transaction_weights: dict[str, int] = Field(default_factory=dict)
    config_path: str = Field(min_length=1)
    jar_path: str = Field(min_length=1)
    results_dir: str = Field(min_length=1)
    java_bin: str = "java"


class ChaosBladeAction(ActionBase):
    type: Literal["chaosblade"] = "chaosblade"
    resource: Literal["cpu", "memory", "disk", "network"]
    command: str = Field(min_length=1)
    duration_sec: int = Field(ge=1, le=86400)
    blade_path: str = Field(min_length=1)
