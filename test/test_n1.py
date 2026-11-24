"""Tests and acceptance checks for the shared Experiment State Machine."""

from __future__ import annotations

import pytest

from db_repro_agent.models.experiment_state import ExperimentPhase, ExperimentState
from db_repro_agent.runtime.orchestrator import Orchestrator
from db_repro_agent.runtime.state_machine import InvalidStateTransition, StateMachine


def state(phase: ExperimentPhase = ExperimentPhase.CREATED) -> ExperimentState:
    return ExperimentState(experiment_id="n1-test", phase=phase, mode="direct")


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExperimentPhase.CREATED, ExperimentPhase.PREPARING),
        (ExperimentPhase.WARMUP, ExperimentPhase.BASELINE),
        (ExperimentPhase.INJECTING, ExperimentPhase.OBSERVING),
        (ExperimentPhase.EVALUATING, ExperimentPhase.CLEANING_UP),
    ],
)
def test_legal_transitions_pass(current: ExperimentPhase, target: ExperimentPhase) -> None:
    machine = StateMachine()

    result = machine.transition(state(current), target)

    assert result.phase == target
    assert result.history[-1].from_phase == current
    assert result.history[-1].to_phase == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExperimentPhase.CREATED, ExperimentPhase.INJECTING),
        (ExperimentPhase.BASELINE, ExperimentPhase.SUCCESS),
        (ExperimentPhase.SUCCESS, ExperimentPhase.INJECTING),
    ],
)
def test_illegal_transitions_are_rejected(current: ExperimentPhase, target: ExperimentPhase) -> None:
    with pytest.raises(InvalidStateTransition):
        StateMachine().transition(state(current), target)


def test_full_direct_lifecycle_uses_one_shared_state_machine() -> None:
    calls: list[ExperimentPhase] = []

    def handler(current: ExperimentState):
        calls.append(current.phase)
        return {"completed": current.phase.value}

    handlers = {phase: handler for phase in Orchestrator._execution_sequence}
    handlers[ExperimentPhase.CLEANING_UP] = handler
    final = Orchestrator().run(state(), handlers)

    assert final.phase == ExperimentPhase.SUCCESS
    assert calls == [*Orchestrator._execution_sequence, ExperimentPhase.CLEANING_UP]
    assert [event.to_phase for event in final.history] == [
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
    ]
    assert final.step_results[ExperimentPhase.BASELINE.value]["completed"] == "baseline"


def test_executor_failure_transitions_to_failed_then_cleanup() -> None:
    calls: list[ExperimentPhase] = []

    def successful_handler(current: ExperimentState):
        calls.append(current.phase)
        return "ok"

    def failing_injection(current: ExperimentState):
        calls.append(current.phase)
        raise RuntimeError("simulated executor failure")

    handlers = {phase: successful_handler for phase in Orchestrator._execution_sequence}
    handlers[ExperimentPhase.INJECTING] = failing_injection
    handlers[ExperimentPhase.CLEANING_UP] = successful_handler

    final = Orchestrator().run(state(), handlers)

    assert final.phase == ExperimentPhase.CLEANING_UP
    assert final.error == "simulated executor failure"
    assert ExperimentPhase.FAILED in [event.to_phase for event in final.history]
    assert final.history[-1].to_phase == ExperimentPhase.CLEANING_UP
    assert calls[-1] == ExperimentPhase.CLEANING_UP
    assert ExperimentPhase.SUCCESS not in [event.to_phase for event in final.history]


def test_state_keeps_action_execution_and_evaluation_without_owning_their_logic() -> None:
    current = state().model_copy(
        update={
            "action": {"action_id": "a1", "type": "sql"},
            "execution_result": {"success": True},
            "evaluation": {"hit": True},
        }
    )

    assert current.phase == ExperimentPhase.CREATED
    assert current.action["action_id"] == "a1"
    assert current.execution_result["success"] is True
    assert current.evaluation["hit"] is True


def test_propagation_mode_uses_same_lifecycle() -> None:
    final = Orchestrator().run(state().model_copy(update={"mode": "propagation"}))

    assert final.phase == ExperimentPhase.SUCCESS
    assert final.mode == "propagation"
