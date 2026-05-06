from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from agent_runtime.skills.base import Skill
from agent_runtime.types import DBContextSummary


class BuildPlannerContextSkill(Skill):
    name = "build_planner_context_skill"

    def execute(
        self,
        request_goal: str,
        allowed_categories: List[str],
        allowed_subtypes: List[str],
        context: DBContextSummary,
        database_topology: str,
    ) -> Dict[str, Any]:
        table_summaries = []
        for table in sorted(context.tables, key=lambda item: item.row_count or 0, reverse=True)[:10]:
            table_summaries.append(
                {
                    "name": table.name,
                    "row_count": table.row_count,
                    "indexes": table.indexes[:12],
                    "columns": [
                        {
                            "name": column.name,
                            "data_type": column.data_type,
                            "indexed": column.indexed,
                            "nullable": column.nullable,
                        }
                        for column in table.columns[:12]
                    ],
                }
            )
        return {
            "goal": request_goal,
            "database": context.database,
            "allowed_categories": allowed_categories,
            "allowed_subtypes": allowed_subtypes,
            "database_topology": database_topology,
            "schema_notes": context.notes,
            "distribution": context.distribution,
            "tables": table_summaries,
        }
