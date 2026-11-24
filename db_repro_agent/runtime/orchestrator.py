"""Generic lifecycle orchestrator that delegates all phase changes to StateMachine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..models.experiment_state import ExperimentPhase, ExperimentState
from .state_machine import StateMachine


PhaseHandler = Callable[[ExperimentState], Any]


class Orchestrator:
    """Run the common lifecycle using injected phase handlers.

    Handlers are deliberately generic in this stage. Future Direct and
    Propagation runners can inject real preparation, execution and evaluation
    modules without taking ownership of phase transitions.
    """

    _execution_sequence = (
        ExperimentPhase.PREPARING,
        ExperimentPhase.WORKLOAD_STARTING,
        ExperimentPhase.WARMUP,
        ExperimentPhase.BASELINE,
        ExperimentPhase.PLANNING,
        ExperimentPhase.VALIDATING,
        ExperimentPhase.INJECTING,
        ExperimentPhase.OBSERVING,
        ExperimentPhase.RECOVERING,
        ExperimentPhase.EVALUATING,
    )

    def __init__(self, state_machine: StateMachine | None = None) -> None:
        self.state_machine = state_machine or StateMachine()

    def run(
        self,
        state: ExperimentState,
        handlers: Mapping[ExperimentPhase, PhaseHandler] | None = None,
    ) -> ExperimentState:
        handlers = handlers or {}
        current = state
        try:
            for phase in self._execution_sequence:
                current = self.state_machine.transition(current, phase)
                handler = handlers.get(phase)
                if handler is not None:
                    current = current.with_step_result(phase, handler(current))
            current = self.state_machine.transition(current, ExperimentPhase.CLEANING_UP)
            cleanup = handlers.get(ExperimentPhase.CLEANING_UP)
            if cleanup is not None:
                current = current.with_step_result(ExperimentPhase.CLEANING_UP, cleanup(current))
            return self.state_machine.transition(current, ExperimentPhase.SUCCESS)
        except Exception as exc:
            if current.phase not in {ExperimentPhase.FAILED, ExperimentPhase.CLEANING_UP}:
                current = self.state_machine.fail(current, exc)
            if current.phase == ExperimentPhase.FAILED:
                current = self.state_machine.transition(current, ExperimentPhase.CLEANING_UP, reason="failure cleanup")
            cleanup = handlers.get(ExperimentPhase.CLEANING_UP)
            if cleanup is not None:
                try:
                    current = current.with_step_result(ExperimentPhase.CLEANING_UP, cleanup(current))
                except Exception as cleanup_error:
                    current = current.model_copy(update={"error": f"{current.error}; cleanup failed: {cleanup_error}"})
            return current
