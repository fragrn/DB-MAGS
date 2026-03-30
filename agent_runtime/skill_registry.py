from __future__ import annotations

from typing import Dict, Iterable

from agent_runtime.skills.base import Skill


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill]):
        self._skills: Dict[str, Skill] = {skill.name: skill for skill in skills}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"Unknown skill: {name}")
        return self._skills[name]

    def list_names(self):
        return sorted(self._skills)
