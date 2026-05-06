from __future__ import annotations

from typing import Dict, List

from agent_runtime.skills.base import Skill


class PrepareSortScanSupportSkill(Skill):
    name = "prepare_sortscan_support_skill"

    TABLES = {
        "order_by": "agent_order_by_support",
        "group_by": "agent_group_by_support",
        "large_table_scan": "agent_large_scan_support",
    }

    def execute(self, database: str) -> Dict[str, object]:
        return {"created_tables": list(self.TABLES.values()), "setup_steps": [], "rollback_steps": []}
