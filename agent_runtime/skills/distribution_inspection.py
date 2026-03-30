from __future__ import annotations

from typing import Dict

from agent_runtime.db import db_cursor
from agent_runtime.skills.base import Skill


class InspectDistributionSkill(Skill):
    name = "inspect_distribution_skill"

    def execute(self, database: str, limit_tables: int = 5, limit_columns: int = 3) -> Dict[str, object]:
        distribution = {"table_samples": [], "notes": []}
        try:
            with db_cursor() as (_conn, cur):
                cur.execute(
                    """
                    SELECT TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_ROWS DESC
                    LIMIT %s
                    """,
                    (database, limit_tables),
                )
                for (table_name,) in cur.fetchall():
                    cur.execute(
                        """
                        SELECT COLUMN_NAME
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                        LIMIT %s
                        """,
                        (database, table_name, limit_columns),
                    )
                    columns = [row[0] for row in cur.fetchall()]
                    distribution["table_samples"].append({"table": table_name, "sampled_columns": columns})
        except Exception as exc:
            distribution["notes"].append(f"distribution inspection unavailable: {exc}")
        return distribution
