from typing import List, Optional, Tuple
from uuid import uuid4

from agent.models import ColumnProfile, DatabaseProfile, TableProfile, TaskSpec
from agent.task_agents.base import BaseTaskAgent


class MissingIndexAgent(BaseTaskAgent):
    name = "missing_index"
    _numeric_types = {"tinyint", "smallint", "mediumint", "int", "bigint", "float", "double", "decimal"}
    _temporal_types = {"date", "datetime", "timestamp"}

    def plan(self, profile: DatabaseProfile, runtime_context: dict) -> List[TaskSpec]:
        candidate = self._pick_candidate(profile, int(runtime_context.get("min_row_count", 1000)))
        if not candidate:
            return []
        table, column = candidate
        predicate_value = self._pick_predicate_value(column)
        predicate = self._build_predicate(column, predicate_value)
        sql = f"SELECT COUNT(*) FROM {self._quote_identifier(table.name)} WHERE {predicate};"
        task_id = f"missing-index-{uuid4().hex[:8]}"
        return [
            TaskSpec(
                task_id=task_id,
                task_type="missing_index_query",
                agent_name=self.name,
                start_after_seconds=int(runtime_context.get("fault_inject_time", 60)),
                duration_seconds=int(runtime_context.get("fault_duration", 60)),
                payload={
                    "mode": "sql",
                    "sql": sql,
                    "repeat": int(runtime_context.get("query_repeat", 10)),
                    "sleep_seconds": float(runtime_context.get("query_sleep", 5.0)),
                },
                metadata={
                    "table": table.name,
                    "column": column.name,
                    "description": "Query injection against a large table column without an index.",
                    "schema": profile.schema_name,
                },
            )
        ]

    def _pick_candidate(self, profile: DatabaseProfile, min_row_count: int) -> Optional[Tuple[TableProfile, ColumnProfile]]:
        ranked = []
        for table in profile.tables:
            if table.row_count_estimate < min_row_count:
                continue
            indexed_prefixes = {cols[0] for cols in table.indexes.values() if cols}
            for column in table.columns:
                if column.name in indexed_prefixes:
                    continue
                if column.column_key in {"PRI", "UNI", "MUL"}:
                    continue
                if column.data_type.lower() not in self._numeric_types | self._temporal_types:
                    continue
                score = table.row_count_estimate
                if column.cardinality_estimate:
                    score += column.cardinality_estimate
                ranked.append((score, table, column))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        _, table, column = ranked[0]
        return table, column

    def _pick_predicate_value(self, column: ColumnProfile):
        if column.min_value is None and column.max_value is None:
            return 1
        if column.data_type.lower() in self._numeric_types:
            if column.min_value is None:
                return column.max_value
            if column.max_value is None:
                return column.min_value
            return (column.min_value + column.max_value) / 2
        return column.min_value or column.max_value

    def _build_predicate(self, column: ColumnProfile, value) -> str:
        column_sql = self._quote_identifier(column.name)
        if column.data_type.lower() in self._numeric_types:
            return f"{column_sql} >= {value}"
        return f"{column_sql} >= '{value}'"

    @staticmethod
    def _quote_identifier(name: str) -> str:
        return "`" + name.replace("`", "``") + "`"
