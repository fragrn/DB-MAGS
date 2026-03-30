from __future__ import annotations

from abc import ABC, abstractmethod

from agent_runtime.types import DBContextSummary, ExperimentRequest, TaskSpec


class BaseTaskAgent(ABC):
    agent_type = "base"

    @abstractmethod
    def prepare(self, context: DBContextSummary, request: ExperimentRequest):
        raise NotImplementedError

    @abstractmethod
    def explain(self, task_spec: TaskSpec) -> str:
        raise NotImplementedError
