from __future__ import annotations

from typing import Dict

from agent_runtime.skills.base import Skill


class PrepareBackupTaskSkill(Skill):
    name = "prepare_backup_task_skill"

    def execute(self, database: str, source_table: str = "orders", backup_table: str = "orders_backup_agent") -> Dict[str, object]:
        create_sql = f"CREATE TABLE {backup_table} AS SELECT * FROM {source_table}"
        drop_sql = f"DROP TABLE IF EXISTS {backup_table}"
        return {
            "title": f"Backup table {source_table} into {backup_table}.",
            "execution_steps": [
                {"kind": "sql", "sql": drop_sql, "database": database},
                {"kind": "sql", "sql": create_sql, "database": database},
            ],
            "rollback_steps": [{"kind": "sql", "sql": drop_sql, "database": database}],
            "expected_signals": ["backup table created"],
        }
