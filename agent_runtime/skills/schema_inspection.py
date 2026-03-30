from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from agent_runtime.db import db_cursor
from agent_runtime.skills.base import Skill
from agent_runtime.types import DBColumnProfile, DBContextSummary, DBTableProfile


class InspectSchemaSkill(Skill):
    name = "inspect_schema_skill"

    def execute(self, database: str) -> DBContextSummary:
        tables: Dict[str, DBTableProfile] = {}
        notes: List[str] = []
        try:
            with db_cursor() as (_conn, cur):
                cur.execute(
                    """
                    SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE,
                           IF(s.INDEX_NAME IS NULL, 0, 1) AS indexed
                    FROM information_schema.COLUMNS c
                    LEFT JOIN information_schema.STATISTICS s
                      ON c.TABLE_SCHEMA = s.TABLE_SCHEMA
                     AND c.TABLE_NAME = s.TABLE_NAME
                     AND c.COLUMN_NAME = s.COLUMN_NAME
                    WHERE c.TABLE_SCHEMA = %s
                    ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
                    """,
                    (database,),
                )
                for table_name, column_name, data_type, nullable, indexed in cur.fetchall():
                    table = tables.setdefault(table_name, DBTableProfile(name=table_name))
                    table.columns.append(
                        DBColumnProfile(
                            name=column_name,
                            data_type=data_type,
                            nullable=nullable == "YES",
                            indexed=bool(indexed),
                        )
                    )

                cur.execute(
                    """
                    SELECT TABLE_NAME, TABLE_ROWS
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                    """,
                    (database,),
                )
                for table_name, table_rows in cur.fetchall():
                    table = tables.setdefault(table_name, DBTableProfile(name=table_name))
                    table.row_count = int(table_rows or 0)

                cur.execute(
                    """
                    SELECT TABLE_NAME, INDEX_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = %s
                    """,
                    (database,),
                )
                idx_map = defaultdict(set)
                for table_name, index_name in cur.fetchall():
                    idx_map[table_name].add(index_name)
                for table_name, indexes in idx_map.items():
                    table = tables.setdefault(table_name, DBTableProfile(name=table_name))
                    table.indexes = sorted(indexes)
        except Exception as exc:
            notes.append(f"schema inspection unavailable: {exc}")

        if not tables:
            notes.append("falling back to static TPCC anomaly catalog")
        return DBContextSummary(database=database, tables=sorted(tables.values(), key=lambda t: t.name), notes=notes)
