from abc import ABC, abstractmethod
from typing import List

from agent.models import DatabaseProfile, TaskSpec


class BaseTaskAgent(ABC):
    name = "base"

    @abstractmethod
    def plan(self, profile: DatabaseProfile, runtime_context: dict) -> List[TaskSpec]:
        raise NotImplementedError
