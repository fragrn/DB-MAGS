"""M9 runner for reproducing a human-specified anomaly propagation graph.

This runner deliberately does not use the experiment StateMachine. M9 is the
manual propagation baseline; it executes only the root Action and treats all
downstream nodes as observations supplied by the metrics/evidence layer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..evaluation.graph_evaluator import GraphEvaluation, GraphEvaluator
from ..evaluation.node_evaluator import NodeObservation
from ..execution.dispatcher import ActionDispatcher
from ..graph.root_selector import select_roots
from ..metrics.sampler import MetricsSampler
from ..metrics.timeline import ExperimentPhase, ExperimentTimeline
from ..models.action import ActionBase, ActionResult, BenchBaseAction
from ..models.anomaly_graph import AnomalyGraph
from ..models.common import ResultStatus


ObservationProvider = Callable[[ExperimentTimeline], list[NodeObservation]]


class PropagationReproductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    graph: AnomalyGraph
    root_action: ActionBase
    background_workload: BenchBaseAction | None = None
    root_nodes: list[str] = Field(default_factory=list)
    baseline_seconds: float = Field(default=1.0, ge=0)
    observe_seconds: float = Field(default=1.0, ge=0)
    recovery_seconds: float = Field(default=0.0, ge=0)


class PropagationRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    incident_id: str
    status: ResultStatus
    root_nodes: list[str]
    root_action_result: ActionResult | None = None
    background_workload_result: ActionResult | None = None
    graph_evaluation: GraphEvaluation | None = None
    first_failed_edge: str | None = None
    node_first_trigger_times: dict[str, float] = Field(default_factory=dict)
    full_graph_success: bool = False
    metrics_sample_count: int = 0
    cleanup_success: bool = False
    background_alive_before_cleanup: bool | None = None
    timeline: Any


class PropagationRunner:
    """Execute only the manually supplied root injection Action."""

    def __init__(
        self,
        *,
        dispatcher: ActionDispatcher,
        sampler: MetricsSampler,
        graph_evaluator: GraphEvaluator | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.sampler = sampler
        self.graph_evaluator = graph_evaluator or GraphEvaluator()

    def run(
        self,
        request: PropagationReproductionRequest,
        *,
        observations: list[NodeObservation] | dict[str, NodeObservation] | None = None,
        observation_provider: ObservationProvider | None = None,
    ) -> PropagationRunResult:
        roots = sorted(request.root_nodes or request.graph.root_nodes or select_roots(request.graph))
        if not roots:
            raise ValueError("propagation graph must have at least one root node")
        graph_roots = set(select_roots(request.graph))
        if not set(roots).issubset(graph_roots):
            raise ValueError("root_nodes must be graph root nodes")
        if request.root_action.target_node not in set(roots):
            raise ValueError("root_action.target_node must be one of root_nodes")

        timeline = ExperimentTimeline()
        root_result: ActionResult | None = None
        background_result: ActionResult | None = None
        background_started = False
        background_alive_before_cleanup: bool | None = None
        cleanup_success = False

        try:
            if request.background_workload is not None:
                background_result = self.dispatcher.benchbase.start(request.background_workload)
                background_started = background_result.success
                if not background_result.success:
                    return self._result(
                        request,
                        roots,
                        timeline,
                        root_result,
                        background_result,
                        observations,
                        observation_provider,
                        cleanup_success=False,
                        background_alive_before_cleanup=False,
                    )

            self.sampler.start(timeline, phase=ExperimentPhase.WARMUP)
            self.sampler.set_phase(ExperimentPhase.BASELINE, name="baseline_start")
            if request.baseline_seconds:
                time.sleep(request.baseline_seconds)

            self.sampler.set_phase(
                ExperimentPhase.INJECTION,
                name="root_injection_start",
                metadata={"action_id": request.root_action.action_id, "root_nodes": roots},
            )
            root_result = self.dispatcher.start(request.root_action)
            if root_result.success:
                root_result = self.dispatcher.execute(request.root_action)
            self.sampler.set_phase(ExperimentPhase.OBSERVING, name="propagation_observing")
            if request.observe_seconds:
                time.sleep(request.observe_seconds)
            self.sampler.set_phase(ExperimentPhase.RECOVERY, name="propagation_recovery")
            if request.recovery_seconds:
                time.sleep(request.recovery_seconds)
        finally:
            self.sampler.stop()
            if request.background_workload is not None and background_started:
                background_alive_before_cleanup = _background_alive(self.dispatcher)
            try:
                self.dispatcher.cleanup(request.root_action)
                if request.background_workload is not None and background_started:
                    self.dispatcher.benchbase.cleanup(request.background_workload)
                cleanup_success = True
            except Exception:
                cleanup_success = False

        return self._result(
            request,
            roots,
            timeline,
            root_result,
            background_result,
            observations,
            observation_provider,
            cleanup_success=cleanup_success,
            background_alive_before_cleanup=background_alive_before_cleanup,
        )

    def _result(
        self,
        request: PropagationReproductionRequest,
        roots: list[str],
        timeline: ExperimentTimeline,
        root_result: ActionResult | None,
        background_result: ActionResult | None,
        observations: list[NodeObservation] | dict[str, NodeObservation] | None,
        observation_provider: ObservationProvider | None,
        *,
        cleanup_success: bool,
        background_alive_before_cleanup: bool | None,
    ) -> PropagationRunResult:
        if observation_provider is not None:
            observations = observation_provider(timeline)
        if observations is None:
            observations = []
        graph_evaluation = self.graph_evaluator.evaluate(request.graph, observations, ignored_nodes=set(roots))
        first_times = _first_trigger_times(observations)
        action_success = root_result is not None and root_result.success
        background_success = request.background_workload is None or (
            background_result is not None and background_result.success
        )
        full_success = (
            action_success
            and background_success
            and background_alive_before_cleanup is not False
            and cleanup_success
            and graph_evaluation.all_nodes_hit
            and graph_evaluation.all_edges_satisfied
        )
        status = ResultStatus.EXPERIMENT_SUCCESS if full_success else (
            ResultStatus.SYSTEM_ERROR if not action_success or not background_success or background_alive_before_cleanup is False or not cleanup_success
            else ResultStatus.EXPERIMENT_MISS
        )
        return PropagationRunResult(
            incident_id=request.incident_id,
            status=status,
            root_nodes=roots,
            root_action_result=root_result,
            background_workload_result=background_result,
            graph_evaluation=graph_evaluation,
            first_failed_edge=graph_evaluation.failed_edge,
            node_first_trigger_times=first_times,
            full_graph_success=full_success,
            metrics_sample_count=len(timeline.samples),
            cleanup_success=cleanup_success,
            background_alive_before_cleanup=background_alive_before_cleanup,
            timeline=timeline,
        )


def _first_trigger_times(
    observations: list[NodeObservation] | dict[str, NodeObservation],
) -> dict[str, float]:
    values = observations.values() if isinstance(observations, dict) else observations
    return {
        observation.node_id: observation.timestamp_sec
        for observation in values
        if observation.hit and observation.timestamp_sec is not None
    }


def _background_alive(dispatcher: ActionDispatcher) -> bool:
    executor = getattr(dispatcher, "benchbase", None)
    is_alive = getattr(executor, "is_alive", None)
    if callable(is_alive):
        return bool(is_alive())
    # Test doubles and remote executors may not expose a process handle. In
    # that case a successful start is the strongest available signal.
    return True
