"""Strict parsing and measurement of LLM-generated executable Actions."""

from __future__ import annotations

import json
from typing import Any, Callable, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import LLMConfig
from ..models.action import (
    ActionBase,
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
)
from ..runtime.action_validator import ActionValidator, ValidationResult
from ..runtime.safety import SafetyChecker
from .context_builder import ActionGenerationContext
from .llm_client import LLMClient, OpenAICompatibleClient


ActionUnion: TypeAlias = SQLAction | TransactionAction | BenchBaseAction | ChaosBladeAction


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionUnion
    rationale: str | None = None


class ActionGenerationError(ValueError):
    """Structured-output parsing failed; the original Action is not modified."""


class ActionGenerator:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    @classmethod
    def from_config(cls, config: LLMConfig) -> "ActionGenerator":
        """Build the production generator from ``AppConfig.llm``."""
        return cls(OpenAICompatibleClient(config))

    def generate(self, context: ActionGenerationContext) -> ActionPlan:
        try:
            raw = self.client.generate_action(context.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            if isinstance(exc, ActionGenerationError):
                raise
            raise ActionGenerationError(f"LLM generation failed: {exc}") from exc
        return self.parse(raw)

    def generate_validated(
        self,
        context: ActionGenerationContext,
        *,
        validator: ActionValidator,
        safety_checker: SafetyChecker,
    ) -> ActionPlan:
        """Generate an ActionPlan and reject it unless both gates pass."""
        plan = self.generate(context)
        result = self.validate_for_execution(
            plan, validator=validator, safety_checker=safety_checker
        )
        if not result.passed:
            details = "; ".join(issue.message for issue in result.issues)
            raise ActionGenerationError(
                f"generated Action rejected before execution: {details or result.decision}"
            )
        return plan

    @staticmethod
    def parse(raw: Any) -> ActionPlan:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ActionGenerationError(f"structured output is not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ActionGenerationError("structured output must be a JSON object")
        payload = raw if "action" in raw else {"action": raw}
        try:
            return ActionPlan.model_validate(payload)
        except ValidationError as exc:
            raise ActionGenerationError(f"structured Action validation failed: {exc}") from exc

    @staticmethod
    def validate(
        plan: ActionPlan,
        validator: ActionValidator,
        safety_checker: SafetyChecker | None = None,
    ) -> ValidationResult:
        """Run database validation, then Safety, without changing the Action."""
        validation = validator.validate(plan.action)
        if not validation.passed or safety_checker is None:
            return validation
        safety = safety_checker.check(plan.action)
        return safety if not safety.passed else validation

    @staticmethod
    def validate_for_execution(
        plan: ActionPlan,
        *,
        validator: ActionValidator,
        safety_checker: SafetyChecker,
    ) -> ValidationResult:
        """Mandatory gate before handing an Action to an Executor."""
        return ActionGenerator.validate(plan, validator, safety_checker)


class ActionBatchStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    structured_output_success: int = Field(ge=0)
    validation_pass: int = Field(ge=0)
    execution_success: int = Field(ge=0)
    anomaly_hit: int = Field(ge=0)

    @property
    def structured_output_success_rate(self) -> float:
        return self.structured_output_success / self.total if self.total else 0.0

    @property
    def validation_pass_rate(self) -> float:
        return self.validation_pass / self.total if self.total else 0.0

    @property
    def execution_success_rate(self) -> float:
        return self.execution_success / self.total if self.total else 0.0

    @property
    def anomaly_hit_rate(self) -> float:
        return self.anomaly_hit / self.total if self.total else 0.0


def evaluate_batch(
    raw_outputs: list[Any],
    *,
    validator: Callable[[ActionBase], bool],
    executor: Callable[[ActionBase], bool],
    oracle: Callable[[ActionBase], bool],
) -> ActionBatchStats:
    """Measure the four M10 rates over a fixed Action fixture set.

    Invalid structured outputs stop at the first stage. Validation, execution,
    and oracle callbacks receive the exact parsed Action and may not rewrite it.
    """
    structured = validation = execution = hit = 0
    for raw in raw_outputs:
        try:
            plan = ActionGenerator.parse(raw)
        except ActionGenerationError:
            continue
        structured += 1
        if not validator(plan.action):
            continue
        validation += 1
        if not executor(plan.action):
            continue
        execution += 1
        if oracle(plan.action):
            hit += 1
    return ActionBatchStats(
        total=len(raw_outputs),
        structured_output_success=structured,
        validation_pass=validation,
        execution_success=execution,
        anomaly_hit=hit,
    )
