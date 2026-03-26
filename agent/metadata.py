from typing import Dict, List, Optional, Sequence, Tuple

from agent.models import ColumnProfile, DatabaseProfile, TableProfile


class MetadataInspector:
    def __init__(self, database=None, sample_limit: int = 128):
        self.database = database or self._default_database()
        self.sample_limit = sample_limit

    def inspect(self, schema_name: Optional[str] = None) -> DatabaseProfile:
        schema = schema_name or self.database.mysql_db
        conn, cur = self.database.connection2()
        try:
            tables = self._load_tables(cur, schema)
            indexes = self._load_indexes(cur, schema)
            columns = self._load_columns(cur, schema)
            table_profiles = []
            for table_name, row_count in tables:
                table_columns = []
                for column_row in columns.get(table_name, []):
                    distribution = self._safe_column_distribution(cur, schema, table_name, column_row[0], column_row[1])
                    table_columns.append(
                        ColumnProfile(
                            name=column_row[0],
                            data_type=column_row[1],
                            is_nullable=column_row[2] == "YES",
                            column_key=column_row[3] or "",
                            cardinality_estimate=distribution.get("distinct_count"),
                            null_ratio=distribution.get("null_ratio"),
                            min_value=distribution.get("min_value"),
                            max_value=distribution.get("max_value"),
                        )
                    )
                table_profiles.append(
                    TableProfile(
                        name=table_name,
                        row_count_estimate=row_count,
                        columns=table_columns,
                        indexes=indexes.get(table_name, {}),
                    )
                )
            return DatabaseProfile(schema_name=schema, tables=table_profiles)
        finally:
            conn.close()

    def _load_tables(self, cur, schema: str) -> Sequence[Tuple[str, int]]:
        cur.execute(
            """
            SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY COALESCE(TABLE_ROWS, 0) DESC, TABLE_NAME ASC
            """,
            (schema,),
        )
        return cur.fetchall()

    def _load_indexes(self, cur, schema: str) -> Dict[str, Dict[str, List[str]]]:
        cur.execute(
            """
            SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """,
            (schema,),
        )
        grouped: Dict[str, Dict[str, List[str]]] = {}
        for table_name, index_name, column_name, _ in cur.fetchall():
            grouped.setdefault(table_name, {}).setdefault(index_name, []).append(column_name)
        return grouped

    def _load_columns(self, cur, schema: str) -> Dict[str, List[Tuple[str, str, str, str]]]:
        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (schema,),
        )
        grouped: Dict[str, List[Tuple[str, str, str, str]]] = {}
        for table_name, column_name, data_type, is_nullable, column_key in cur.fetchall():
            grouped.setdefault(table_name, []).append((column_name, data_type, is_nullable, column_key))
        return grouped

    def _safe_column_distribution(self, cur, schema: str, table: str, column: str, data_type: str) -> Dict[str, Optional[object]]:
        if data_type.lower() not in {"tinyint", "smallint", "mediumint", "int", "bigint", "float", "double", "decimal", "date", "datetime", "timestamp"}:
            return {}
        identifier = self._quote_identifier
        query = (
            f"SELECT MIN({identifier(column)}), MAX({identifier(column)}), COUNT(DISTINCT {identifier(column)}), "
            f"SUM(CASE WHEN {identifier(column)} IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) "
            f"FROM {identifier(schema)}.{identifier(table)}"
        )
        try:
            cur.execute(query)
            min_value, max_value, distinct_count, null_ratio = cur.fetchone()
        except Exception:
            return {}
        return {
            "min_value": min_value,
            "max_value": max_value,
            "distinct_count": int(distinct_count) if distinct_count is not None else None,
            "null_ratio": float(null_ratio) if null_ratio is not None else None,
        }

    @staticmethod
    def _quote_identifier(name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    @staticmethod
    def _default_database():
        from Connection.Connection import Database

        return Database()
