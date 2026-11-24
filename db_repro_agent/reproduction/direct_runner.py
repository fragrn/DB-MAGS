"""M7 Direct Reproduction minimal closed loop without an Agent."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..evaluation.incident_evaluator import IncidentEvaluation, IncidentEvaluator
from ..execution.dispatcher import ActionDispatcher
from ..metrics.sampler import MetricsSampler
from ..metrics.timeline import ExperimentPhase, ExperimentTimeline
from ..models.action import ActionBase, ActionResult, BenchBaseAction
from ..models.common import ResultStatus
from ..models.evidence import EvidenceSnapshot, EvidenceSnapshotPair, SnapshotBoundary
from ..models.experiment_state import ExperimentPhase as StatePhase
from ..models.experiment_state import ExperimentState
from ..runtime.orchestrator import Orchestrator


class DirectReproductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    action: ActionBase
    background_workload: BenchBaseAction | None = None
    oracle_rule_id: str = Field(min_length=1)
    baseline_seconds: float = Field(default=1.0, ge=0)
    observe_seconds: float = Field(default=1.0, ge=0)
    recovery_seconds: float = Field(default=0.0, ge=0)
    severity_rule_id: str | None = None
    supporting_rule_ids: list[str] = Field(default_factory=list)


class DirectRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    incident_id: str
    status: ResultStatus
    state: ExperimentState
    action_result: ActionResult | None = None
    background_workload_result: ActionResult | None = None
    evaluation: IncidentEvaluation | None = None
    timeline: Any
    pre_snapshot: EvidenceSnapshot | None = None
    post_snapshot: EvidenceSnapshot | None = None


class ExecutionFailure(RuntimeError):
    def __init__(self, result: ActionResult) -> None:
        super().__init__(result.error_message or "action execution failed")
        self.result = result


class ExperimentMiss(RuntimeError):
    def __init__(self, evaluation: IncidentEvaluation) -> None:
        super().__init__(evaluation.reason)
        self.evaluation = evaluation


class DirectRunner:
    """Run a manually specified Action through the shared lifecycle."""

    def __init__(
        self,
        *,
        dispatcher: ActionDispatcher,
        sampler: MetricsSampler,
        evaluator: IncidentEvaluator,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.sampler = sampler
        self.evaluator = evaluator
        self.orchestrator = orchestrator or Orchestrator()

    def run(self, request: DirectReproductionRequest) -> DirectRunResult:
        timeline = ExperimentTimeline()
        state = ExperimentState(experiment_id=request.incident_id, mode="direct", action=request.action.model_dump(mode="json"))
        action_result: ActionResult | None = None
        background_workload_result: ActionResult | None = None
        background_workload_started = False
        evaluation: IncidentEvaluation | None = None
        pre_snapshot: EvidenceSnapshot | None = None
        post_snapshot: EvidenceSnapshot | None = None

        def preparing(current: ExperimentState):
            return {"status": "prepared"}

        def workload_starting(current: ExperimentState):
            nonlocal background_workload_result, background_workload_started
            workload = request.background_workload
            if workload is None:
                return {"status": "no_background_workload"}
            if workload.benchmark.lower() not in {"tpcc", "tpch"}:
                raise ValueError("M7 background workload must use the TPCC or TPCH BenchBase benchmark")

            background_workload_result = self.dispatcher.benchbase.start(workload)
            background_workload_started = background_workload_result.success
            if not background_workload_result.success:
                raise ExecutionFailure(background_workload_result)
            timeline.mark(
                ExperimentPhase.WARMUP,
                name="background_workload_started",
                metadata={"benchmark": workload.benchmark, "action_id": workload.action_id},
            )
            return {
                "status": "background_started",
                "benchmark": workload.benchmark,
                "result": background_workload_result.model_dump(mode="json"),
            }

        def warmup(current: ExperimentState):
            self.sampler.start(timeline, phase=ExperimentPhase.WARMUP)
            return {"seconds": 0.0}

        def baseline(current: ExperimentState):
            timeline.mark(ExperimentPhase.BASELINE, name="baseline_start")
            self.sampler.set_phase(ExperimentPhase.BASELINE, name="baseline_start")
            if request.baseline_seconds:
                time.sleep(request.baseline_seconds)
            return {"seconds": request.baseline_seconds}

        def planning(current: ExperimentState):
            return {"action_id": request.action.action_id}

        def validating(current: ExperimentState):
            self.dispatcher.validate(request.action)
            return {"validated": True}

        def injecting(current: ExperimentState):
            nonlocal pre_snapshot
            pre_snapshot = self.sampler.capture_snapshot(SnapshotBoundary.PRE_INJECTION)
            timeline.mark(ExperimentPhase.INJECTION, name="injection_start", metadata={"action_id": request.action.action_id})
            self.sampler.set_phase(ExperimentPhase.INJECTION, name="injection_start", metadata={"action_id": request.action.action_id})
            started = self.dispatcher.start(request.action)
            if not started.success:
                nonlocal action_result
                action_result = started
                raise ExecutionFailure(started)
            return started.model_dump(mode="json")

        def observing(current: ExperimentState):
            nonlocal action_result, post_snapshot
            self.sampler.set_phase(ExperimentPhase.OBSERVING, name="observation_start")
            action_result = self.dispatcher.execute(request.action)
            timeline.add_sample("action_duration_ms", action_result.duration_ms, phase=ExperimentPhase.OBSERVING)
            post_snapshot = self.sampler.capture_snapshot(SnapshotBoundary.POST_ACTION)
            if not action_result.success:
                raise ExecutionFailure(action_result)
            if request.observe_seconds:
                time.sleep(request.observe_seconds)
            return action_result.model_dump(mode="json")

        def recovering(current: ExperimentState):
            self.sampler.set_phase(ExperimentPhase.RECOVERY, name="recovery_start")
            if request.recovery_seconds:
                time.sleep(request.recovery_seconds)
            return {"seconds": request.recovery_seconds}

        def evaluating(current: ExperimentState):
            nonlocal evaluation
            self.sampler.stop()
            evaluation = self.evaluator.evaluate(
                incident_id=request.incident_id,
                oracle_rule_id=request.oracle_rule_id,
                timeline=timeline,
                phase=ExperimentPhase.OBSERVING,
                baseline_phase=ExperimentPhase.BASELINE,
                snapshots=EvidenceSnapshotPair(pre_injection=pre_snapshot, post_action=post_snapshot)
                if pre_snapshot is not None and post_snapshot is not None
                else None,
                severity_rule_id=request.severity_rule_id,
                supporting_rule_ids=request.supporting_rule_ids,
            )
            if not evaluation.hit:
                raise ExperimentMiss(evaluation)
            return evaluation.model_dump(mode="json")

        def cleanup(current: ExperimentState):
            self.sampler.stop()
            self.dispatcher.cleanup(request.action)
            workload = request.background_workload
            if workload is not None and background_workload_started:
                self.dispatcher.benchbase.cleanup(workload)
            return {
                "cleaned": True,
                "background_workload_cleaned": workload is not None and background_workload_started,
            }

        handlers = {
            StatePhase.PREPARING: preparing,
            StatePhase.WORKLOAD_STARTING: workload_starting,
            StatePhase.WARMUP: warmup,
            StatePhase.BASELINE: baseline,
            StatePhase.PLANNING: planning,
            StatePhase.VALIDATING: validating,
            StatePhase.INJECTING: injecting,
            StatePhase.OBSERVING: observing,
            StatePhase.RECOVERING: recovering,
            StatePhase.EVALUATING: evaluating,
            StatePhase.CLEANING_UP: cleanup,
        }
        final_state = self.orchestrator.run(state, handlers)
        if evaluation is not None:
            final_state = final_state.model_copy(update={"evaluation": evaluation.model_dump(mode="json")})
        if action_result is not None:
            final_state = final_state.model_copy(update={"execution_result": action_result.model_dump(mode="json")})

        if background_workload_result is not None and not background_workload_result.success:
            status = ResultStatus.SYSTEM_ERROR
        elif action_result is not None and not action_result.success:
            status = ResultStatus.SYSTEM_ERROR
        elif evaluation is not None and not evaluation.hit:
            status = ResultStatus.EXPERIMENT_MISS
        elif final_state.phase == StatePhase.SUCCESS:
            status = ResultStatus.EXPERIMENT_SUCCESS
        else:
            status = ResultStatus.SYSTEM_ERROR
        return DirectRunResult(
            incident_id=request.incident_id,
            status=status,
            state=final_state,
            action_result=action_result,
            background_workload_result=background_workload_result,
            evaluation=evaluation,
            timeline=timeline,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
        )
