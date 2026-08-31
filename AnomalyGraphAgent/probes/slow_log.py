"""Incremental MySQL slow-log capture shared by experiment runtimes."""

from __future__ import annotations

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
_THREAD_ID = re.compile(r"\bId:\s*(\d+)", re.I)
_USE_DATABASE = re.compile(r"^use\s+(`?)([^`;]+)\1\s*;?$", re.I)
_VARIABLE_NAMES = (
    "slow_query_log",
    "slow_query_log_file",
    "log_output",
    "long_query_time",
    "log_queries_not_using_indexes",
    "min_examined_row_limit",
)


class SlowLogProbe:
    """Collect entries appended after a FILE or mysql.slow_log marker."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def marker(self) -> dict[str, Any]:
        variables, variable_error = self.variables()
        return self._marker_from_variables(variables, variable_error)

    def start_capture(self) -> dict[str, Any]:
        """Enable slow logging when needed, then establish an injection marker."""
        original, original_error = self.variables()
        enabled_by_probe = False
        enable_error = ""
        if not _is_enabled(original.get("slow_query_log")):
            enable_error = self._set_slow_query_log(True)
            enabled_by_probe = not enable_error

        injection_variables, injection_error = self.variables()
        marker = self._marker_from_variables(injection_variables, injection_error)
        marker.update({
            "original_variables": original,
            "original_variable_error": original_error,
            "variables_at_injection_start": injection_variables,
            "enabled_by_probe": enabled_by_probe,
            "enable_error": enable_error,
        })
        return marker

    def collect(
        self,
        marker: dict[str, Any],
        target_database: str | None = None,
    ) -> dict[str, Any]:
        variables, variable_error = self.variables()
        outputs = {part.strip().upper() for part in str(variables.get("log_output") or "").split(",")}
        file_result = (
            self._collect_file(marker.get("file") or {}, variables)
            if "FILE" in outputs
            else {"path": "", "readable": False, "entries": [], "error": "", "skipped": True}
        )
        table_entries, table_error = (
            self._collect_table(marker.get("table") or {})
            if "TABLE" in outputs
            else ([], "")
        )

        entries: list[dict[str, Any]] = []
        source = "none"
        if "TABLE" in outputs and table_entries:
            entries = table_entries
            source = "TABLE"
        elif "FILE" in outputs and file_result.get("entries"):
            entries = list(file_result["entries"])
            source = "FILE"
        elif table_entries:
            entries = table_entries
            source = "TABLE"
        elif file_result.get("entries"):
            entries = list(file_result["entries"])
            source = "FILE"

        injection_variables = marker.get("variables_at_injection_start") or marker.get("variables") or {}
        annotated = [_annotate_reason(dict(entry), injection_variables) for entry in entries]
        target_entries = [
            entry for entry in annotated
            if target_database is None or _same_database(entry.get("database"), target_database)
        ]
        file_available = "FILE" in outputs and bool(file_result.get("readable"))
        table_available = "TABLE" in outputs and table_error == ""
        available = _is_enabled(variables.get("slow_query_log")) and (file_available or table_available)
        return {
            "available": available,
            "source": source,
            "entries": annotated,
            "entry_count": len(annotated),
            "target_database": target_database,
            "target_entries": target_entries,
            "target_entry_count": len(target_entries),
            "matched": available and bool(target_entries),
            "variables_before": injection_variables,
            "variables_after": variables,
            "variables_at_injection_start": injection_variables,
            "variables_at_injection_end": variables,
            "variable_error": variable_error,
            "enable_error": marker.get("enable_error", ""),
            "file": file_result,
            "table_error": table_error,
        }

    def restore(self, marker: dict[str, Any]) -> dict[str, Any]:
        """Restore only the slow_query_log switch changed by start_capture."""
        error = ""
        if marker.get("enabled_by_probe"):
            original = marker.get("original_variables") or {}
            error = self._set_slow_query_log(_is_enabled(original.get("slow_query_log")))
        variables, variable_error = self.variables()
        return {
            "restored": not error,
            "changed_by_probe": bool(marker.get("enabled_by_probe")),
            "error": error,
            "variables_after_restore": variables,
            "variable_error": variable_error,
        }

    def variables(self) -> tuple[dict[str, Any], str]:
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    placeholders = ",".join(["%s"] * len(_VARIABLE_NAMES))
                    cursor.execute(
                        f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({placeholders})",
                        _VARIABLE_NAMES,
                    )
                    values = {str(row["Variable_name"]): row["Value"] for row in cursor.fetchall()}
                return values, ""
            finally:
                connection.close()
        except Exception as exc:
            return {}, str(exc)

    # Backward-compatible private name used by InputAnalysisAgent.
    def _variables(self) -> tuple[dict[str, Any], str]:
        return self.variables()

    def _marker_from_variables(self, variables: dict[str, Any], variable_error: str) -> dict[str, Any]:
        path = str(variables.get("slow_query_log_file") or "")
        outputs = {part.strip().upper() for part in str(variables.get("log_output") or "").split(",")}
        if "TABLE" in outputs:
            table_marker, table_error = self._table_marker()
        else:
            table_marker, table_error = {"skipped": True}, ""
        return {
            "variables": variables,
            "variable_error": variable_error,
            "file": self._file_marker(path) if "FILE" in outputs else {"path": path, "skipped": True},
            "table": table_marker,
            "table_error": table_error,
        }

    def _set_slow_query_log(self, enabled: bool) -> str:
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SET GLOBAL slow_query_log = {'ON' if enabled else 'OFF'}")
                return ""
            finally:
                connection.close()
        except Exception as exc:
            return str(exc)

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
                        cursor.execute(
                            """
                            SELECT start_time, query_time, lock_time, rows_sent,
                                   rows_examined, db, sql_text, thread_id
                            FROM mysql.slow_log
                            ORDER BY start_time
                            LIMIT %s, 10000
                            """,
                            (int(marker.get("row_count") or 0),),
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
            return {
                "path": str(file_path),
                "exists": False,
                "readable": False,
                "offset": 0,
                "inode": None,
                "error": str(exc),
            }

    def _collect_file(self, marker: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
        path = str(variables.get("slow_query_log_file") or marker.get("path") or "")
        if not path:
            return {"path": "", "readable": False, "entries": [], "error": "slow_query_log_file is empty"}
        file_path = Path(path)
        try:
            stat = file_path.stat()
            same_file = marker.get("inode") is not None and int(marker.get("inode")) == stat.st_ino
            prior_offset = int(marker.get("offset") or 0)
            offset = prior_offset if same_file and stat.st_size >= prior_offset else 0
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
    active_database = ""
    pending_thread_id = 0

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
        thread_match = _THREAD_ID.search(raw_line)
        if thread_match:
            pending_thread_id = int(thread_match.group(1))
        match = _QUERY_TIME.search(raw_line)
        if match:
            finish()
            current = {
                "query_time_sec": float(match.group(1)),
                "lock_time_sec": float(match.group(2)),
                "rows_sent": int(match.group(3)),
                "rows_examined": int(match.group(4)),
                "database": active_database,
                "thread_id": pending_thread_id,
            }
            continue
        if current is None:
            continue
        line = raw_line.strip()
        database_match = _USE_DATABASE.match(line)
        if database_match:
            active_database = database_match.group(2)
            current["database"] = active_database
            continue
        if line.startswith("#") or line.startswith("SET timestamp="):
            continue
        sql_lines.append(line)
    finish()
    return entries


def _annotate_reason(entry: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    try:
        threshold = float(variables.get("long_query_time", 10))
    except (TypeError, ValueError):
        threshold = 10.0
    query_time = float(entry.get("query_time_sec") or 0.0)
    if query_time >= threshold:
        reason = "query_time_gte_long_query_time"
    elif _is_enabled(variables.get("log_queries_not_using_indexes")):
        reason = "possible_log_queries_not_using_indexes"
    else:
        reason = "unknown"
    entry["logging_reason"] = reason
    entry["long_query_time_sec_at_injection"] = threshold
    entry["log_queries_not_using_indexes_at_injection"] = variables.get("log_queries_not_using_indexes")
    return entry


def _same_database(value: Any, target: str) -> bool:
    return str(value or "").strip("`").lower() == str(target).strip("`").lower()


def _is_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "on", "true", "yes"}


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
