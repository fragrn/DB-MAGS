"""Incremental MySQL slow-log evidence collection for mechanism evaluation."""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent.config import RuntimeConfig


_QUERY_TIME = re.compile(
    r"#\s*Query_time:\s*([0-9.]+).*?Lock_time:\s*([0-9.]+).*?"
    r"Rows_sent:\s*(\d+).*?Rows_examined:\s*(\d+)",
    re.I,
)


class SlowLogProbe:
    """Collect entries appended after a marker from mysql.slow_log or FILE output."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def marker(self) -> dict[str, Any]:
        variables, variable_error = self._variables()
        path = str(variables.get("slow_query_log_file") or "")
        file_marker = self._file_marker(path)
        table_marker, table_error = self._table_marker()
        return {
            "variables": variables,
            "variable_error": variable_error,
            "file": file_marker,
            "table": table_marker,
            "table_error": table_error,
        }

    def collect(self, marker: dict[str, Any]) -> dict[str, Any]:
        variables, variable_error = self._variables()
        file_result = self._collect_file(marker.get("file") or {}, variables)
        table_entries, table_error = self._collect_table(marker.get("table") or {})

        entries: list[dict[str, Any]] = []
        source = "none"
        if table_entries:
            entries = table_entries
            source = "TABLE"
        elif file_result.get("entries"):
            entries = list(file_result["entries"])
            source = "FILE"

        return {
            "available": bool(entries) or bool(file_result.get("readable")) or table_error == "",
            "source": source,
            "entries": entries,
            "entry_count": len(entries),
            "variables_before": marker.get("variables") or {},
            "variables_after": variables,
            "variable_error": variable_error,
            "file": file_result,
            "table_error": table_error,
        }

    def _connect(self, *, database: str | None = None):
        import pymysql
        from pymysql.cursors import DictCursor

        return pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
            database=database,
            cursorclass=DictCursor,
            connect_timeout=5,
            read_timeout=10,
            autocommit=True,
        )

    def _variables(self) -> tuple[dict[str, Any], str]:
        names = (
            "slow_query_log",
            "slow_query_log_file",
            "log_output",
            "long_query_time",
            "log_queries_not_using_indexes",
            "min_examined_row_limit",
        )
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    placeholders = ",".join(["%s"] * len(names))
                    cursor.execute(
                        f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({placeholders})",
                        names,
                    )
                    values = {str(row["Variable_name"]): row["Value"] for row in cursor.fetchall()}
                return values, ""
            finally:
                connection.close()
        except Exception as exc:
            return {}, str(exc)

    def _table_marker(self) -> tuple[dict[str, Any], str]:
        try:
            connection = self._connect(database="mysql")
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT MAX(start_time) AS max_start_time, COUNT(*) AS row_count FROM mysql.slow_log")
                    row = cursor.fetchone() or {}
                value = row.get("max_start_time")
                return {
                    "max_start_time": value.isoformat(sep=" ") if hasattr(value, "isoformat") else str(value or ""),
                    "row_count": int(row.get("row_count") or 0),
                }, ""
            finally:
                connection.close()
        except Exception as exc:
            return {}, str(exc)

    def _collect_table(self, marker: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        try:
            connection = self._connect(database="mysql")
            try:
                with connection.cursor() as cursor:
                    start = str(marker.get("max_start_time") or "")
                    if start:
                        cursor.execute(
                            """
                            SELECT start_time, query_time, lock_time, rows_sent,
                                   rows_examined, db, sql_text, thread_id
                            FROM mysql.slow_log
                            WHERE start_time > %s
                            ORDER BY start_time
                            LIMIT 10000
                            """,
                            (start,),
                        )
                    else:
                        baseline_count = int(marker.get("row_count") or 0)
                        cursor.execute(
                            """
                            SELECT start_time, query_time, lock_time, rows_sent,
                                   rows_examined, db, sql_text, thread_id
                            FROM mysql.slow_log
                            ORDER BY start_time
                            LIMIT %s, 10000
                            """,
                            (baseline_count,),
                        )
                    rows = list(cursor.fetchall())
                return [self._normalize_table_entry(row) for row in rows], ""
            finally:
                connection.close()
        except Exception as exc:
            return [], str(exc)

    @staticmethod
    def _normalize_table_entry(row: dict[str, Any]) -> dict[str, Any]:
        start = row.get("start_time")
        return {
            "start_time": start.isoformat(sep=" ") if hasattr(start, "isoformat") else str(start or ""),
            "query_time_sec": _seconds(row.get("query_time")),
            "lock_time_sec": _seconds(row.get("lock_time")),
            "rows_sent": int(row.get("rows_sent") or 0),
            "rows_examined": int(row.get("rows_examined") or 0),
            "database": _decode(row.get("db")),
            "sql": _decode(row.get("sql_text")),
            "thread_id": int(row.get("thread_id") or 0),
            "source": "TABLE",
        }

    @staticmethod
    def _file_marker(path: str) -> dict[str, Any]:
        if not path:
            return {"path": "", "exists": False, "readable": False, "offset": 0, "inode": None}
        file_path = Path(path)
        try:
            stat = file_path.stat()
            return {
                "path": str(file_path),
                "exists": True,
                "readable": os.access(file_path, os.R_OK),
                "offset": stat.st_size,
                "inode": stat.st_ino,
            }
        except Exception as exc:
            return {"path": str(file_path), "exists": False, "readable": False, "offset": 0, "inode": None, "error": str(exc)}

    def _collect_file(self, marker: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
        path = str(variables.get("slow_query_log_file") or marker.get("path") or "")
        if not path:
            return {"path": "", "readable": False, "entries": [], "error": "slow_query_log_file is empty"}
        file_path = Path(path)
        try:
            stat = file_path.stat()
            same_file = marker.get("inode") is not None and int(marker.get("inode")) == stat.st_ino
            offset = int(marker.get("offset") or 0) if same_file and stat.st_size >= int(marker.get("offset") or 0) else 0
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                text = handle.read()
            return {
                "path": str(file_path),
                "readable": True,
                "same_file": same_file,
                "start_offset": offset,
                "end_offset": stat.st_size,
                "entries": parse_slow_log(text),
                "error": "",
            }
        except Exception as exc:
            return {"path": str(file_path), "readable": False, "entries": [], "error": str(exc)}


def parse_slow_log(text: str) -> list[dict[str, Any]]:
    """Parse standard MySQL/Percona FILE slow-log entries."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    sql_lines: list[str] = []

    def finish() -> None:
        nonlocal current, sql_lines
        if current is None:
            return
        current["sql"] = "\n".join(line for line in sql_lines if line).strip()
        current["source"] = "FILE"
        entries.append(current)
        current = None
        sql_lines = []

    for raw_line in text.splitlines():
        match = _QUERY_TIME.search(raw_line)
        if match:
            finish()
            current = {
                "query_time_sec": float(match.group(1)),
                "lock_time_sec": float(match.group(2)),
                "rows_sent": int(match.group(3)),
                "rows_examined": int(match.group(4)),
            }
            continue
        if current is None:
            continue
        line = raw_line.strip()
        if line.startswith("#") or line.startswith("SET timestamp=") or line.startswith("use "):
            continue
        sql_lines.append(line)
    finish()
    return entries


def evaluate_slow_log_evidence(evidence: dict[str, Any], blueprint: dict[str, Any]) -> dict[str, Any]:
    """Deterministically evaluate fast unindexed queries recorded in the slow log."""
    entries = list(evidence.get("entries") or [])
    threshold = _intended_long_query_time(blueprint, evidence)
    target_sql = _target_sql_statements(blueprint)
    fast = [entry for entry in entries if float(entry.get("query_time_sec", 0)) < threshold]
    fast_scans = [entry for entry in fast if int(entry.get("rows_examined", 0)) > 0]
    matching = [entry for entry in fast_scans if _matches_target(entry.get("sql", ""), target_sql)]
    setting_enabled = _planned_setting_enabled(blueprint, "log_queries_not_using_indexes")
    calibration_queries = ((blueprint.get("data_spec") or {}).get("calibration_queries") or [])
    calibration_matched = bool((evidence.get("calibration") or {}).get("matched", not calibration_queries))
    total = len(entries)
    symptom_hit = bool(fast)
    mechanism_hit = bool(matching) and setting_enabled and calibration_matched
    return {
        "applicable": is_slow_log_reproduction(blueprint),
        "available": bool(evidence.get("available")),
        "source": evidence.get("source", "none"),
        "long_query_time_sec": threshold,
        "new_entry_count": total,
        "fast_entry_count": len(fast),
        "fast_entry_ratio": round(len(fast) / total, 4) if total else 0.0,
        "fast_scan_entry_count": len(fast_scans),
        "matching_target_entry_count": len(matching),
        "log_queries_not_using_indexes_planned": setting_enabled,
        "calibration_matched": calibration_matched,
        "symptom_hit": symptom_hit,
        "mechanism_hit": mechanism_hit,
        "success": symptom_hit and mechanism_hit,
        "reason": (
            "Fast target queries with rows examined were recorded while log_queries_not_using_indexes was enabled."
            if symptom_hit and mechanism_hit
            else "No matching fast unindexed target query was observed in incremental slow-log evidence."
        ),
    }


def is_slow_log_reproduction(blueprint: dict[str, Any]) -> bool:
    text = json.dumps(
        {
            "incident": blueprint.get("incident_spec", {}),
            "evaluation": blueprint.get("evaluation_spec", {}),
            "tasks": blueprint.get("task_specs", []),
        },
        ensure_ascii=False,
    ).lower()
    return any(token in text for token in ("slow log", "slow_log", "slow-query-log", "log_queries_not_using_indexes"))


def _planned_setting_enabled(blueprint: dict[str, Any], variable: str) -> bool:
    text = json.dumps(blueprint.get("task_specs", []), ensure_ascii=False).lower()
    pattern = rf"{re.escape(variable.lower())}\s*(?:=|,|\s)\s*['\"]?(?:on|1|true)"
    return bool(re.search(pattern, text))


def _intended_long_query_time(blueprint: dict[str, Any], evidence: dict[str, Any]) -> float:
    text = json.dumps(blueprint.get("task_specs", []), ensure_ascii=False).lower()
    match = re.search(r"long_query_time\s*(?:=|,|\s)\s*['\"]?([0-9.]+)", text)
    if match:
        return max(0.000001, float(match.group(1)))
    variables = evidence.get("variables_after") or evidence.get("variables_before") or {}
    try:
        return max(0.000001, float(variables.get("long_query_time", 10)))
    except (TypeError, ValueError):
        return 10.0


def _target_sql_statements(blueprint: dict[str, Any]) -> list[str]:
    statements: list[str] = []
    for task in blueprint.get("task_specs", []) or []:
        for action in task.get("actions", []) or []:
            if action.get("kind") == "raw_sql_workload" and action.get("sql"):
                statements.append(str(action["sql"]))
            if action.get("kind") == "raw_transaction_script":
                for script in action.get("scripts", []) or []:
                    for step in script.get("steps", []) or []:
                        sql = step if isinstance(step, str) else (step or {}).get("sql")
                        if sql and re.match(r"^\s*(SELECT|WITH)\b", str(sql), re.I):
                            statements.append(str(sql))
    return statements


def _matches_target(entry_sql: str, target_sql: list[str]) -> bool:
    if not target_sql:
        return False
    normalized_entry = _normalize_sql(entry_sql)
    return any(_normalize_sql(sql) in normalized_entry or normalized_entry in _normalize_sql(sql) for sql in target_sql)


def _normalize_sql(sql: str) -> str:
    value = re.sub(r"\s+", " ", str(sql)).strip().rstrip(";").lower()
    return value


def _seconds(value: Any) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
