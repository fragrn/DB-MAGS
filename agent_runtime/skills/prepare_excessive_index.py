from __future__ import annotations

from typing import Dict, List

from agent_runtime.skills.base import Skill


class PrepareExcessiveIndexSkill(Skill):
    name = "prepare_excessive_index_skill"

    def execute(self, database: str, support_table: str = "agent_excessive_index") -> Dict[str, object]:
        setup = [
            {
                "kind": "sql",
                "sql": f"CREATE TABLE IF NOT EXISTS {support_table} AS SELECT o_id, o_c_id, o_carrier_id, o_ol_cnt FROM orders LIMIT 50000",
                "database": database,
            },
        ]
        query = f"UPDATE {support_table} SET o_ol_cnt = o_ol_cnt + 1 WHERE o_c_id > 1000"
        rollback = [
            {"kind": "sql", "sql": f"DROP TABLE IF EXISTS {support_table}", "database": database},
        ]
        return {
            "title": "Create redundant indexes and run update workload.",
            "setup_steps": setup,
            "query": query,
            "rollback_steps": rollback,
            "expected_signals": ["index maintenance overhead"],
        }
