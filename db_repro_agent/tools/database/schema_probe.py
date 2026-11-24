"""MySQL schema, index and row-count probes."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from .base import ToolResult, utc_now


class SchemaProbe:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def probe_schema(self, database: str) -> ToolResult:
        started = utc_now()
        sql = """
            SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT,
                   IS_NULLABLE, DATA_TYPE, COLUMN_TYPE, COLUMN_KEY, EXTRA
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
        try:
            with closing(self.adapter.connect(database=database)) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, (database,))
                    rows = list(cursor.fetchall())
            tables: dict[str, dict[str, Any]] = {}
            for row in rows:
                table = row["TABLE_NAME"]
                tables.setdefault(table, {"table_name": table, "columns": []})["columns"].append(
                    {
                        "name": row["COLUMN_NAME"],
                        "ordinal_position": row["ORDINAL_POSITION"],
                        "default": row["COLUMN_DEFAULT"],
                        "nullable": row["IS_NULLABLE"] == "YES",
                        "data_type": row["DATA_TYPE"],
                        "column_type": row["COLUMN_TYPE"],
                        "column_key": row["COLUMN_KEY"],
                        "extra": row["EXTRA"],
                    }
                )
            return ToolResult.ok({"database": database, "tables": list(tables.values())}, started, utc_now())
        except Exception as exc:
            return _failure(exc, started)

    def probe_indexes(self, database: str, table: str) -> ToolResult:
        started = utc_now()
        sql = """
            SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME,
                   COLLATION, CARDINALITY, INDEX_TYPE
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
        """
        try:
            with closing(self.adapter.connect(database=database)) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, (database, table))
                    rows = list(cursor.fetchall())
            indexes = [
                {
                    "name": row["INDEX_NAME"],
                    "unique": not bool(row["NON_UNIQUE"]),
                    "sequence": row["SEQ_IN_INDEX"],
                    "column": row["COLUMN_NAME"],
                    "collation": row["COLLATION"],
                    "cardinality": row["CARDINALITY"],
                    "index_type": row["INDEX_TYPE"],
                }
                for row in rows
            ]
            return ToolResult.ok({"database": database, "table": table, "indexes": indexes}, started, utc_now())
        except Exception as exc:
            return _failure(exc, started)

    def probe_row_counts(self, database: str) -> ToolResult:
        started = utc_now()
        sql = """
            SELECT TABLE_NAME, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
        """
        try:
            with closing(self.adapter.connect(database=database)) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, (database,))
                    rows = list(cursor.fetchall())
            counts = {row["TABLE_NAME"]: row["TABLE_ROWS"] for row in rows}
            return ToolResult.ok({"database": database, "row_counts": counts}, started, utc_now())
        except Exception as exc:
            return _failure(exc, started)


def _failure(exc: Exception, started) -> ToolResult:
    from .mysql import _error_result

    return _error_result(exc, started, utc_now())
