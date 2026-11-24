"""Chain Agent action generation and isolated M11 Reflection retries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .action_generator import ActionGenerator, ActionPlan
from .chain_reflector import ChainReflector
from .context_builder import ActionGenerationContext, ContextBuilder
from .contracts import ChainReproductionRequest, ExperimentResult
from ..models.reflection import AttemptOutcome, AttemptRecord, ReflectionContext, ReflectionResult, ReflectionRunResult
from ..runtime.action_validator import ActionValidator
from ..runtime.artifact_store import ArtifactStore
from ..runtime.safety import SafetyChecker


class ChainAgent:
    """Shared Action-generation entry point for a human-specified chain.

    M10 stops after ActionPlan generation and validation. Actual propagation
    execution remains the responsibility of the M12/M9 reproduction pipeline.
    """

    def __init__(
        self,
        action_generator: ActionGenerator,
        *,
        context_builder: ContextBuilder | None = None,
        reflector: ChainReflector | None = None,
    ) -> None:
        self.action_generator = action_generator
        self.context_builder = context_builder or ContextBuilder()
        self.reflector = reflector or ChainReflector()

    def build_context(self, request: ChainReproductionRequest) -> ActionGenerationContext:
        return self.context_builder.build(
            target_anomaly=request.target_node,
            target_node=request.target_node,
            database=request.database,
            schema=request.schema_,
            indexes=request.indexes,
            data_scale=request.data_scale,
            runtime_snapshot=request.runtime_snapshot,
            workload_status=request.workload_status,
            propagation_chain=request.propagation_chain,
            active_injection_nodes=request.active_injection_nodes,
            observed_downstream_nodes=request.observed_downstream_nodes,
            duration_budget_seconds=request.duration_budget_seconds,
            safety_constraints=request.safety_constraints,
            previous_reflection=request.previous_reflection,
        )

    def generate_action_plan(self, request: ChainReproductionRequest) -> ActionPlan:
        return self.action_generator.generate(self.build_context(request))

    def reproduce_chain(
        self,
        request: ChainReproductionRequest,
        *,
        validator: ActionValidator,
        safety_checker: SafetyChecker,
    ) -> ExperimentResult:
        plan = self.generate_action_plan(request)
        validation = self.action_generator.validate_for_execution(
            plan, validator=validator, safety_checker=safety_checker
        )
        if not validation.passed:
            return ExperimentResult(
                incident_id=request.incident_id,
                agent="chain",
                status="rejected",
                action_plan=plan,
                validation=validation,
                message="ActionPlan rejected before execution",
            )
        return ExperimentResult(
            incident_id=request.incident_id,
            agent="chain",
            status="action_generated",
            action_plan=plan,
            validation=validation,
            message="M10 generated and validated an ActionPlan; execution is deferred",
        )

    def reproduce_chain_with_reflection(
        self,
        request: ChainReproductionRequest,
        *,
        attempt_runner: Callable[[ActionPlan, int], AttemptOutcome | dict[str, Any]],
        validator: ActionValidator,
        safety_checker: SafetyChecker,
        max_retry_rounds: int,
        artifact_store: ArtifactStore | None = None,
        experiment: str | Path | None = None,
    ) -> ReflectionRunResult:
        """Run bounded Chain-only retries; every new plan passes both gates."""
        if max_retry_rounds < 0:
            raise ValueError("max_retry_rounds must be >= 0")
        current_request = request
        records: list[AttemptRecord] = []
        previous_actions: list[dict[str, Any]] = []

        for attempt in range(1, max_retry_rounds + 2):
            plan = self.generate_action_plan(current_request)
            validation = self.action_generator.validate_for_execution(
                plan, validator=validator, safety_checker=safety_checker
            )
            if validation.passed:
                raw_outcome = attempt_runner(plan, attempt)
                outcome = raw_outcome if isinstance(raw_outcome, AttemptOutcome) else AttemptOutcome.model_validate(raw_outcome)
            else:
                outcome = AttemptOutcome(
                    success=False,
                    action_result={
                        "action_id": plan.action.action_id,
                        "success": False,
                        "error": "Action failed validation or safety checks",
                    },
                )

            record = AttemptRecord(attempt=attempt, action_plan=plan, validation=validation, outcome=outcome)
            if outcome.success:
                records.append(record)
                self._persist_attempt(artifact_store, experiment, record)
                return ReflectionRunResult(
                    success=True,
                    attempts=records,
                    final_action_plan=plan,
                    message="Chain reproduction succeeded",
                )

            action_dict = plan.action.model_dump(mode="json")
            previous_actions.append(action_dict)
            reflection_context = ReflectionContext(
                source="chain",
                target=current_request.target_node,
                previous_actions=previous_actions,
                action_results=[outcome.action_result],
                metrics_summary=outcome.metrics_summary,
                node_results=outcome.node_results,
                edge_results=outcome.edge_results,
                failed_node=outcome.failed_node,
                failed_edge=outcome.failed_edge,
                environment=outcome.environment,
                remaining_budget=current_request.duration_budget_seconds,
            )
            reflection = self.reflector.reflect(reflection_context, attempt=attempt)
            record = record.model_copy(update={"reflection": reflection})
            records.append(record)
            self._persist_attempt(artifact_store, experiment, record)
            if attempt > max_retry_rounds or not reflection.retryable:
                return ReflectionRunResult(
                    success=False,
                    attempts=records,
                    final_action_plan=plan,
                    final_failed_node=reflection.failed_node,
                    final_failed_edge=reflection.failed_edge,
                    message="Chain reproduction exhausted Reflection retries",
                )
            current_request = current_request.model_copy(
                update={"previous_reflection": reflection.model_dump(mode="json")}
            )

        raise AssertionError("bounded Reflection loop unexpectedly exited")

    @staticmethod
    def _persist_attempt(
        artifact_store: ArtifactStore | None,
        experiment: str | Path | None,
        record: AttemptRecord,
    ) -> None:
        if artifact_store is None or experiment is None:
            return
        attempt_dir = artifact_store.create_attempt_dir(Path(experiment) / "chain", record.attempt)
        artifact_store.write_json(attempt_dir / "action_plan.json", record.action_plan)
        artifact_store.write_json(attempt_dir / "action_result.json", record.outcome)
        artifact_store.write_json(attempt_dir / "evaluation.json", {
            "node_results": record.outcome.node_results,
            "edge_results": record.outcome.edge_results,
            "failed_node": record.outcome.failed_node,
            "failed_edge": record.outcome.failed_edge,
        })
        if record.reflection is not None:
            artifact_store.write_json(attempt_dir / "reflection.json", record.reflection)
