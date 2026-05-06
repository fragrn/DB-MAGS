from __future__ import annotations

from typing import Dict, List

from agent_runtime.skills.base import Skill


class PrepareLockSQLSkill(Skill):
    name = "prepare_lock_sql_skill"

    def execute(
        self,
        anomaly_subtype: str,
        database: str,
        duration_seconds: int = 5,
        column_name: str = "agent_meta_lock_run",
    ) -> Dict[str, object]:
        mapping = {
            "record_lock": {
                "sql": "UPDATE new_orders SET no_o_id = no_o_id + 0 WHERE no_w_id = 1 AND no_d_id = 1 AND no_o_id > 10",
                "kind": "hold_sql",
                "rollback": [],
                "title": "Hold a row-level write lock on new_orders.",
            },
            "table_lock": {
                "sql": "LOCK TABLES new_orders WRITE",
                "kind": "hold_sql",
                "rollback": [{"kind": "sql", "sql": "UNLOCK TABLES", "database": database}],
                "title": "Acquire a WRITE lock on new_orders.",
            },
            "metadata_lock": {
                "sql": f"ALTER TABLE new_orders ADD COLUMN {column_name} CHAR(2)",
                "kind": "sql",
                "rollback": [
                    {
                        "kind": "sql",
                        "sql": f"ALTER TABLE new_orders DROP COLUMN {column_name}",
                        "database": database,
                    }
                ],
                "title": "Trigger a metadata lock via ALTER TABLE.",
            },
        }
        payload = mapping[anomaly_subtype]
        execution_steps: List[Dict[str, object]] = [
            {
                "kind": payload["kind"],
                "sql": payload["sql"],
                "database": database,
                "hold_seconds": duration_seconds,
            }
        ]
        return {
            "title": payload["title"],
            "execution_steps": execution_steps,
            "rollback_steps": payload["rollback"],
        }
