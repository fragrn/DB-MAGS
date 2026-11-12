"""
MySQL environment probe: schema, indexes, constraints, table stats,
DB metrics, workload, and EXPLAIN.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator

import pymysql
from pymysql.cursors import DictCursor


class MySQLProbe:
    """Wraps all MySQL introspection queries."""

    def __init__(self, database: str, host: str = "localhost",
                 port: int = 3306, user: str = "root", password: str = ""):
        self.db = database
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    @contextmanager
    def cursor(self) -> Generator[DictCursor, None, None]:
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, database=self.db,
            cursorclass=DictCursor, connect_timeout=30,
        )
        try:
            yield conn.cursor()
        finally:
            conn.commit()
            conn.close()

    # -------------------------------------------------------------------------
    # Basic introspection
    # -------------------------------------------------------------------------

    def version(self) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT VERSION() as v")
            return str(cur.fetchone()["v"])

    def schema(self) -> dict[str, dict]:
        """Return per-table column metadata from information_schema."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                       IS_NULLABLE, COLUMN_KEY, COLUMN_TYPE, COLUMN_DEFAULT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (self.db,),
            )
            tables: dict[str, dict] = {}
            for row in cur.fetchall():
                tname = row["TABLE_NAME"]
                tables.setdefault(tname, {"columns": []})
                tables[tname]["columns"].append({
                    "name": row["COLUMN_NAME"],
                    "type": row["DATA_TYPE"],
                    "nullable": row["IS_NULLABLE"] == "YES",
                    "key": row["COLUMN_KEY"],
                    "column_type": row["COLUMN_TYPE"],
                    "default": row["COLUMN_DEFAULT"],
                })
            return tables

    def indexes(self) -> dict[str, list[dict]]:
        """Return per-table index information."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME,
                       NON_UNIQUE, SEQ_IN_INDEX
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
                """,
                (self.db,),
            )
            result: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                tname = row["TABLE_NAME"]
                result.setdefault(tname, [])
                result[tname].append({
                    "index_name": row["INDEX_NAME"],
                    "column": row["COLUMN_NAME"],
                    "non_unique": bool(row["NON_UNIQUE"]),
                    "seq": row["SEQ_IN_INDEX"],
                })
            return result

    def constraints(self) -> dict[str, list[dict]]:
        """Return primary keys and foreign keys."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME,
                       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s
                  AND (REFERENCED_TABLE_NAME IS NOT NULL OR CONSTRAINT_NAME = 'PRIMARY')
                ORDER BY TABLE_NAME
                """,
                (self.db,),
            )
            result: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                tname = row["TABLE_NAME"]
                result.setdefault(tname, []).append({
                    "column": row["COLUMN_NAME"],
                    "constraint": row["CONSTRAINT_NAME"],
                    "ref_table": row.get("REFERENCED_TABLE_NAME"),
                    "ref_column": row.get("REFERENCED_COLUMN_NAME"),
                })
            return result

    def table_stats(self) -> list[dict]:
        """Return table sizes ordered by row count descending."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH,
                       AVG_ROW_LENGTH, TABLE_COLLATION
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_ROWS DESC
                """,
                (self.db,),
            )
            return list(cur.fetchall())

    # -------------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------------

    _STATUS_VARS = [
        "Threads_connected", "Threads_running", "Slow_queries",
        "Innodb_row_lock_waits", "Innodb_row_lock_time_avg",
        "Innodb_row_lock_time", "Innodb_deadlocks",
        "Innodb_lock_wait_timeouts",
        "Com_select", "Com_insert", "Com_update", "Com_delete",
        "Created_tmp_disk_tables", "Sort_merge_passes",
        "Qcom_begin", "Aborted_connects", "Max_used_connections",
    ]

    def db_metrics(self) -> dict[str, Any]:
        """Return selected global status variables, variables, and processlist."""
        result: dict[str, Any] = {}

        with self.cursor() as cur:
            vars_list = ", ".join(f'"{v}"' for v in self._STATUS_VARS)
            cur.execute(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({vars_list})")
            for row in cur.fetchall():
                result[row["Variable_name"]] = row["Value"]

            cur.execute("SHOW GLOBAL VARIABLES WHERE Variable_name IN ('max_connections','wait_timeout','innodb_lock_wait_timeout')")
            for row in cur.fetchall():
                result[row["Variable_name"]] = row["Value"]

            cur.execute("SHOW FULL PROCESSLIST")
            result["processlist"] = list(cur.fetchall())

        # Extract max_connections safely
        try:
            result["max_connections"] = int(result.get("max_connections", 100))
        except (ValueError, TypeError):
            result["max_connections"] = 100

        return result

    def workload_probe(self, interval_sec: float = 3.0) -> dict[str, Any]:
        """Measure QPS and TPS over a short interval."""
        import time
        with self.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Com_select','Com_insert','Com_update','Com_delete')")
            before = {r["Variable_name"]: int(r["Value"]) for r in cur.fetchall()}
        time.sleep(interval_sec)
        with self.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Com_select','Com_insert','Com_update','Com_delete')")
            after = {r["Variable_name"]: int(r["Value"]) for r in cur.fetchall()}
        delta = {k: after[k] - before[k] for k in before}
        qps = sum(delta.get(k, 0) for k in ["Com_select"]) / interval_sec
        tps = sum(delta.get(k, 0) for k in ["Com_insert", "Com_update", "Com_delete"]) / interval_sec
        return {"qps": round(qps, 2), "tps": round(tps, 2), "interval_sec": interval_sec}

    def explain(self, sql: str) -> list[dict]:
        """Run EXPLAIN on a SQL statement."""
        try:
            with self.cursor() as cur:
                cur.execute(f"EXPLAIN {sql}")
                return list(cur.fetchall())
        except Exception as e:
            return [{"error": str(e)}]

    def latency_sample(self, sql: str, n: int = 5) -> dict[str, Any]:
        """Run a SQL n times and collect latency statistics."""
        import time
        latencies: list[float] = []
        for _ in range(n):
            try:
                t0 = time.perf_counter()
                with self.cursor() as cur:
                    cur.execute(sql)
                    cur.fetchall()
                latencies.append((time.perf_counter() - t0) * 1000)
            except Exception:
                pass
        if not latencies:
            return {"error": "query failed"}
        latencies.sort()
        n50 = int(len(latencies) * 0.5)
        n95 = int(len(latencies) * 0.95)
        return {
            "count": len(latencies),
            "min_ms": round(latencies[0], 3),
            "p50_ms": round(latencies[n50], 3),
            "p95_ms": round(latencies[n95], 3),
            "max_ms": round(latencies[-1], 3),
        }
