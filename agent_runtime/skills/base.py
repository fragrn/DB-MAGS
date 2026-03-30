from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class Skill(ABC):
    name = "skill"
    input_schema: Dict[str, Any] = {}
    output_schema: Dict[str, Any] = {}

    @abstractmethod
    def execute(self, **kwargs):
        raise NotImplementedError
