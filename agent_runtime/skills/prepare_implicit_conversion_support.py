from __future__ import annotations

from typing import Dict

from agent_runtime.skills.base import Skill


class PrepareImplicitConversionSupportSkill(Skill):
    name = "prepare_implicit_conversion_support_skill"

    def execute(self, database: str) -> Dict[str, object]:
        return {
            "created_tables": ["agent_implicit_conversion_support"],
            "setup_steps": [],
            "rollback_steps": [],
        }
