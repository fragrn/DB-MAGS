from __future__ import annotations

from abc import ABC, abstractmethod

from agent_runtime.types import DBContextSummary, ExperimentRequest, PlannedAnomaly, TaskSpec


class BaseTaskAgent(ABC):
    agent_type = "base"

    @abstractmethod
    def prepare(
        self,
        context: DBContextSummary,
        request: ExperimentRequest,
        planner_tasks: list[PlannedAnomaly] | None = None,
    ):
        raise NotImplementedError

    @abstractmethod
    def explain(self, task_spec: TaskSpec) -> str:
        raise NotImplementedError
