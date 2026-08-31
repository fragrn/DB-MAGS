"""
MySQL environment probe: schema, indexes, constraints, table stats,
DB metrics, workload, and EXPLAIN.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator

import pymysql
from pymysql.cursors import DictCursor


def compare_statement_histograms(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Calculate one interval's conservative slow-statement execution ratio."""
    threshold = before.get("slow_threshold_sec")
    if not before.get("available") or not after.get("available") or threshold is None:
        errors = [str(item.get("error")) for item in (before, after) if item.get("error")]
        return {
            "available": False,
            "schema": before.get("schema") or after.get("schema"),
            "slow_threshold_sec": threshold,
            "error": "; ".join(errors) or "performance_schema histogram unavailable",
        }

    before_counts = {
        (int(bucket["timer_low_ps"]), int(bucket["timer_high_ps"])): int(bucket.get("count") or 0)
        for bucket in before.get("buckets", [])
    }
    after_counts = {
        (int(bucket["timer_low_ps"]), int(bucket["timer_high_ps"])): int(bucket.get("count") or 0)
        for bucket in after.get("buckets", [])
    }
    threshold_ps = float(threshold) * 1_000_000_000_000.0
    total_count = 0
    slow_count = 0
    effective_threshold_ps: int | None = None
    for bounds in sorted(set(before_counts) | set(after_counts)):
        delta = after_counts.get(bounds, 0) - before_counts.get(bounds, 0)
        if delta < 0:
            return {
                "available": False,
                "schema": before.get("schema") or after.get("schema"),
                "slow_threshold_sec": float(threshold),
                "reset_detected": True,
                "error": "performance_schema histogram counter decreased",
            }
        total_count += delta
        if bounds[0] >= threshold_ps:
            slow_count += delta
            if effective_threshold_ps is None:
                effective_threshold_ps = bounds[0]

    return {
        "available": True,
        "schema": before.get("schema") or after.get("schema"),
        "slow_threshold_sec": float(threshold),
        "effective_threshold_sec": (
            effective_threshold_ps / 1_000_000_000_000.0
            if effective_threshold_ps is not None
            else None
        ),
        "total_statement_count": total_count,
        "slow_statement_count": slow_count,
        "slow_ratio": (slow_count / total_count) if total_count > 0 else 0.0,
        "reset_detected": False,
    }


def compare_statement_digest_latency(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Calculate per-digest latency deltas for one observation window."""
    if not before.get("available") or not after.get("available"):
        errors = [str(item.get("error")) for item in (before, after) if item.get("error")]
        return {
            "available": False,
            "schema": before.get("schema") or after.get("schema"),
            "error": "; ".join(errors) or "performance_schema digest latency unavailable",
            "top10": [],
            "overall": {},
        }

    before_rows = {
        str(row.get("digest") or ""): row
        for row in before.get("digests", [])
        if row.get("digest")
    }
    after_rows = {
        str(row.get("digest") or ""): row
        for row in after.get("digests", [])
        if row.get("digest")
    }
    before_hist = _digest_histogram_map(before.get("histograms", []))
    after_hist = _digest_histogram_map(after.get("histograms", []))
    rows: list[dict[str, Any]] = []
    total_count = 0
    total_latency_ps = 0
    aggregate_buckets: dict[tuple[int, int], int] = {}

    for digest, after_row in after_rows.items():
        before_row = before_rows.get(digest, {})
        count_delta = int(after_row.get("count_star") or 0) - int(before_row.get("count_star") or 0)
        sum_delta = int(after_row.get("sum_timer_wait_ps") or 0) - int(before_row.get("sum_timer_wait_ps") or 0)
        if count_delta < 0 or sum_delta < 0:
            return {
                "available": False,
                "schema": before.get("schema") or after.get("schema"),
                "reset_detected": True,
                "error": "performance_schema digest counter decreased",
                "top10": [],
                "overall": {},
            }
        if count_delta <= 0:
            continue
        bucket_deltas = _histogram_delta(before_hist.get(digest, {}), after_hist.get(digest, {}))
        if bucket_deltas is None:
            return {
                "available": False,
                "schema": before.get("schema") or after.get("schema"),
                "reset_detected": True,
                "error": "performance_schema digest histogram counter decreased",
                "top10": [],
                "overall": {},
            }
        for bounds, delta in bucket_deltas.items():
            aggregate_buckets[bounds] = aggregate_buckets.get(bounds, 0) + delta
        median_ps = _percentile_from_buckets(bucket_deltas, 0.50)
        p95_ps = _percentile_from_buckets(bucket_deltas, 0.95)
        avg_ps = sum_delta / count_delta if count_delta else 0.0
        if bucket_deltas:
            max_ps = max(high or low for low, high in bucket_deltas)
            max_source = "window_histogram_bucket_high"
        else:
            max_ps = int(after_row.get("max_timer_wait_ps") or 0)
            max_source = "cumulative_digest_max"
        rows.append({
            "digest": digest,
            "digest_text": after_row.get("digest_text") or before_row.get("digest_text") or "",
            "execution_count": count_delta,
            "avg_latency_ms": _ps_to_ms(avg_ps),
            "median_latency_ms": _ps_to_ms(median_ps) if median_ps is not None else None,
            "p95_latency_ms": _ps_to_ms(p95_ps) if p95_ps is not None else None,
            "max_latency_ms": _ps_to_ms(max_ps) if max_ps else None,
            "max_latency_source": max_source,
            "total_latency_ms": _ps_to_ms(sum_delta),
        })
        total_count += count_delta
        total_latency_ps += sum_delta

    rows.sort(key=lambda item: float(item.get("max_latency_ms") or item.get("p95_latency_ms") or item.get("avg_latency_ms") or 0.0), reverse=True)
    overall_median_ps = _percentile_from_buckets(aggregate_buckets, 0.50)
    overall_p95_ps = _percentile_from_buckets(aggregate_buckets, 0.95)
    return {
        "available": True,
        "schema": after.get("schema") or before.get("schema"),
        "top10": rows[:top_n],
        "overall": {
            "count": total_count,
            "avg_latency_ms": _ps_to_ms(total_latency_ps / total_count) if total_count else 0.0,
            "median_latency_ms": _ps_to_ms(overall_median_ps) if overall_median_ps is not None else None,
            "p95_latency_ms": _ps_to_ms(overall_p95_ps) if overall_p95_ps is not None else None,
            "max_latency_ms": max((float(row.get("max_latency_ms") or 0.0) for row in rows), default=0.0),
            "total_latency_ms": _ps_to_ms(total_latency_ps),
        },
        "reset_detected": False,
    }


def _digest_histogram_map(rows: list[dict[str, Any]]) -> dict[str, dict[tuple[int, int], int]]:
    result: dict[str, dict[tuple[int, int], int]] = {}
    for row in rows:
        digest = str(row.get("digest") or "")
        if not digest:
            continue
        bounds = (int(row.get("timer_low_ps") or 0), int(row.get("timer_high_ps") or 0))
        result.setdefault(digest, {})[bounds] = int(row.get("count") or 0)
    return result


def _histogram_delta(
    before: dict[tuple[int, int], int],
    after: dict[tuple[int, int], int],
) -> dict[tuple[int, int], int] | None:
    result: dict[tuple[int, int], int] = {}
    for bounds in set(before) | set(after):
        delta = after.get(bounds, 0) - before.get(bounds, 0)
        if delta < 0:
            return None
        if delta > 0:
            result[bounds] = delta
    return result


def _percentile_from_buckets(buckets: dict[tuple[int, int], int], percentile: float) -> int | None:
    total = sum(max(0, count) for count in buckets.values())
    if total <= 0:
        return None
    target = max(1, int(total * percentile + 0.999999))
    cumulative = 0
    for (low, high), count in sorted(buckets.items()):
        cumulative += max(0, count)
        if cumulative >= target:
            return high or low
    return None


def _ps_to_ms(value: float | int) -> float:
    return round(float(value) / 1_000_000_000.0, 3)


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
        "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
        "Innodb_log_waits",
        "Com_select", "Com_insert", "Com_update", "Com_delete", "Com_commit",
        "Created_tmp_tables", "Created_tmp_disk_tables", "Sort_merge_passes",
        "Binlog_cache_disk_use",
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

        processlist = result.get("processlist") or []
        metadata_waits = [
            row for row in processlist
            if "metadata lock" in str(row.get("State") or "").lower()
        ]
        result["metadata_lock_wait_count"] = len(metadata_waits)
        result["metadata_lock_evidence"] = (
            "; ".join(str(row.get("Info") or row.get("State") or "")[:160] for row in metadata_waits[:3])
            if metadata_waits
            else ""
        )

        # Extract max_connections safely
        try:
            result["max_connections"] = int(result.get("max_connections", 100))
        except (ValueError, TypeError):
            result["max_connections"] = 100

        return result

    def workload_probe(
        self,
        interval_sec: float = 3.0,
        slow_threshold_sec: float | None = None,
    ) -> dict[str, Any]:
        """Measure QPS and TPS over a short interval."""
        import time
        histogram_before = self.statement_histogram_snapshot(slow_threshold_sec=slow_threshold_sec)
        digest_before = self.statement_digest_latency_snapshot()
        interval_threshold_sec = histogram_before.get("slow_threshold_sec")
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
        histogram_after = self.statement_histogram_snapshot(slow_threshold_sec=interval_threshold_sec)
        digest_after = self.statement_digest_latency_snapshot()
        return {
            "qps": round(qps, 2),
            "tps": round(tps, 2),
            "interval_sec": interval_sec,
            "performance_schema_slow_sql": compare_statement_histograms(
                histogram_before,
                histogram_after,
            ),
            "performance_schema_query_latency": compare_statement_digest_latency(
                digest_before,
                digest_after,
            ),
        }

    def statement_histogram_snapshot(self, slow_threshold_sec: float | None = None) -> dict[str, Any]:
        """Read cumulative statement latency buckets for the configured schema."""
        try:
            with self.cursor() as cur:
                # Keep the introspection query itself out of the target schema's digest set.
                cur.execute("USE performance_schema")
                if slow_threshold_sec is None:
                    cur.execute("SELECT @@GLOBAL.long_query_time AS slow_threshold_sec")
                    row = cur.fetchone() or {}
                    slow_threshold_sec = float(row.get("slow_threshold_sec") or 0.0)
                cur.execute(
                    """
                    SELECT BUCKET_TIMER_LOW, BUCKET_TIMER_HIGH, SUM(COUNT_BUCKET) AS bucket_count
                    FROM performance_schema.events_statements_histogram_by_digest
                    WHERE SCHEMA_NAME = %s
                    GROUP BY BUCKET_TIMER_LOW, BUCKET_TIMER_HIGH
                    ORDER BY BUCKET_TIMER_LOW
                    """,
                    (self.db,),
                )
                buckets = [
                    {
                        "timer_low_ps": int(row["BUCKET_TIMER_LOW"]),
                        "timer_high_ps": int(row["BUCKET_TIMER_HIGH"]),
                        "count": int(row["bucket_count"] or 0),
                    }
                    for row in cur.fetchall()
                ]
            return {
                "available": True,
                "schema": self.db,
                "slow_threshold_sec": float(slow_threshold_sec),
                "buckets": buckets,
            }
        except Exception as exc:
            return {
                "available": False,
                "schema": self.db,
                "slow_threshold_sec": slow_threshold_sec,
                "buckets": [],
                "error": str(exc),
            }

    def statement_digest_latency_snapshot(self) -> dict[str, Any]:
        """Read cumulative per-digest latency counters and histogram buckets."""
        try:
            with self.cursor() as cur:
                cur.execute("USE performance_schema")
                cur.execute(
                    """
                    SELECT
                        SCHEMA_NAME,
                        DIGEST,
                        DIGEST_TEXT,
                        COUNT_STAR,
                        SUM_TIMER_WAIT,
                        AVG_TIMER_WAIT,
                        MAX_TIMER_WAIT
                    FROM performance_schema.events_statements_summary_by_digest
                    WHERE SCHEMA_NAME = %s
                      AND DIGEST IS NOT NULL
                    """,
                    (self.db,),
                )
                digests = [
                    {
                        "schema": row.get("SCHEMA_NAME"),
                        "digest": row.get("DIGEST"),
                        "digest_text": row.get("DIGEST_TEXT"),
                        "count_star": int(row.get("COUNT_STAR") or 0),
                        "sum_timer_wait_ps": int(row.get("SUM_TIMER_WAIT") or 0),
                        "avg_timer_wait_ps": int(row.get("AVG_TIMER_WAIT") or 0),
                        "max_timer_wait_ps": int(row.get("MAX_TIMER_WAIT") or 0),
                    }
                    for row in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT
                        DIGEST,
                        BUCKET_TIMER_LOW,
                        BUCKET_TIMER_HIGH,
                        COUNT_BUCKET
                    FROM performance_schema.events_statements_histogram_by_digest
                    WHERE SCHEMA_NAME = %s
                      AND DIGEST IS NOT NULL
                    """,
                    (self.db,),
                )
                histograms = [
                    {
                        "digest": row.get("DIGEST"),
                        "timer_low_ps": int(row.get("BUCKET_TIMER_LOW") or 0),
                        "timer_high_ps": int(row.get("BUCKET_TIMER_HIGH") or 0),
                        "count": int(row.get("COUNT_BUCKET") or 0),
                    }
                    for row in cur.fetchall()
                ]
            return {
                "available": True,
                "schema": self.db,
                "digests": digests,
                "histograms": histograms,
            }
        except Exception as exc:
            return {
                "available": False,
                "schema": self.db,
                "digests": [],
                "histograms": [],
                "error": str(exc),
            }

    def long_query_time_sec(self) -> float | None:
        """Return MySQL's global slow-query threshold without changing it."""
        try:
            with self.cursor() as cur:
                cur.execute("SELECT @@GLOBAL.long_query_time AS slow_threshold_sec")
                row = cur.fetchone() or {}
            return float(row.get("slow_threshold_sec"))
        except Exception:
            return None

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
