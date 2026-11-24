"""Explicit lifecycle transition table for experiments."""

from __future__ import annotations

from typing import Mapping

from ..models.experiment_state import ExperimentPhase, ExperimentState, StateTransition


class InvalidStateTransition(RuntimeError):
    """Raised when a caller attempts a transition not in the lifecycle graph."""


_PIPELINE = (
    ExperimentPhase.CREATED,
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
    ExperimentPhase.CLEANING_UP,
    ExperimentPhase.SUCCESS,
)


class StateMachine:
    """Single owner of phase changes for both reproduction modes."""

    def __init__(self, transitions: Mapping[ExperimentPhase, set[ExperimentPhase]] | None = None) -> None:
        self.transitions = dict(transitions or _default_transitions())

    def can_transition(self, current: ExperimentPhase, target: ExperimentPhase) -> bool:
        return target in self.transitions.get(current, set())

    def transition(
        self,
        state: ExperimentState,
        target: ExperimentPhase,
        *,
        reason: str | None = None,
    ) -> ExperimentState:
        if not self.can_transition(state.phase, target):
            raise InvalidStateTransition(
                f"illegal experiment transition: {state.phase.value} -> {target.value}"
            )
        event = StateTransition(from_phase=state.phase, to_phase=target, reason=reason)
        return state.model_copy(update={"phase": target, "history": [*state.history, event]})

    def fail(self, state: ExperimentState, error: Exception | str) -> ExperimentState:
        message = str(error)
        failed = self.transition(state, ExperimentPhase.FAILED, reason=message)
        return failed.model_copy(update={"error": message})


def _default_transitions() -> dict[ExperimentPhase, set[ExperimentPhase]]:
    transitions: dict[ExperimentPhase, set[ExperimentPhase]] = {}
    for current, target in zip(_PIPELINE, _PIPELINE[1:]):
        transitions.setdefault(current, set()).add(target)
    # CREATED can fail during initialization; all actual execution phases can fail.
    for phase in _PIPELINE[:-2]:
        transitions.setdefault(phase, set()).add(ExperimentPhase.FAILED)
    transitions[ExperimentPhase.FAILED] = {ExperimentPhase.CLEANING_UP}
    return transitions
