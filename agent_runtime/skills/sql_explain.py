from __future__ import annotations

from typing import Dict

from agent_runtime.db import db_cursor
from agent_runtime.skills.base import Skill


class ExplainSQLSkill(Skill):
    name = "explain_sql_skill"

    def execute(self, sql: str, database: str | None = None) -> Dict[str, object]:
        try:
            with db_cursor(database=database) as (_conn, cur):
                cur.execute(f"EXPLAIN {sql}")
                rows = cur.fetchall()
                return {"validated": True, "rows": rows[:10]}
        except Exception as exc:
            return {"validated": False, "error": str(exc)}
