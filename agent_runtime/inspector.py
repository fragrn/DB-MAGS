from __future__ import annotations

import os
from typing import Any, Dict

from agent_runtime.db import db_cursor
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, EnvironmentSnapshot, ExperimentRequest


class OneShotEnvironmentInspector:
    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def inspect(self, request: ExperimentRequest, context: DBContextSummary | None = None) -> EnvironmentSnapshot:
        database = request.target_database
        context = context or self.skills.get("inspect_schema_skill").execute(database=database)
        if not context.distribution:
            context.distribution = self.skills.get("inspect_distribution_skill").execute(database=database)

        snapshot = EnvironmentSnapshot(
            database=database,
            schema=self._schema_payload(context),
            index_info={table.name: table.indexes for table in context.tables},
            table_stats={table.name: {"row_count": table.row_count or 0} for table in context.tables},
            workload_status={"type": request.workload_config.get("type", "tpcc"), "probe_workload": bool(request.test_enabled)},
            config={"risk_level": request.risk_level, "target_mode": request.target_mode},
            notes=list(context.notes),
        )
        snapshot.dbms = "mysql"
        snapshot.version = self._db_version(snapshot)
        snapshot.db_metrics = self._db_metrics(snapshot)
        snapshot.os_metrics = self._os_metrics(snapshot)
        return snapshot

    @staticmethod
    def _schema_payload(context: DBContextSummary) -> Dict[str, Any]:
        return {
            table.name: {
                "row_count": table.row_count or 0,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.nullable,
                        "indexed": col.indexed,
                    }
                    for col in table.columns
                ],
                "indexes": table.indexes,
            }
            for table in context.tables
        }

    @staticmethod
    def _db_version(snapshot: EnvironmentSnapshot) -> str:
        try:
            with db_cursor(database=snapshot.database) as (_conn, cur):
                cur.execute("SELECT VERSION()")
                row = cur.fetchone()
                return str(row[0]) if row else ""
        except Exception as exc:
            snapshot.notes.append(f"db version probe unavailable: {exc}")
            return ""

    @staticmethod
    def _db_metrics(snapshot: EnvironmentSnapshot) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {}
        try:
            with db_cursor(database=snapshot.database) as (_conn, cur):
                for name in ("Threads_connected", "Threads_running", "Slow_queries", "Innodb_row_lock_waits", "Connections"):
                    cur.execute("SHOW GLOBAL STATUS LIKE %s", (name,))
                    row = cur.fetchone()
                    if row:
                        metrics[name.lower()] = _coerce_number(row[1])
                cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                row = cur.fetchone()
                if row:
                    metrics["max_connections"] = _coerce_number(row[1])
        except Exception as exc:
            snapshot.notes.append(f"db runtime metrics probe unavailable: {exc}")
        return metrics

    @staticmethod
    def _os_metrics(snapshot: EnvironmentSnapshot) -> Dict[str, Any]:
        try:
            import psutil  # type: ignore

            return {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_io": psutil.disk_io_counters()._asdict() if psutil.disk_io_counters() else {},
                "network_io": psutil.net_io_counters()._asdict() if psutil.net_io_counters() else {},
                "load_average": os.getloadavg() if hasattr(os, "getloadavg") else (),
            }
        except Exception as exc:
            snapshot.notes.append(f"os metrics probe unavailable: {exc}")
            try:
                return {"load_average": os.getloadavg() if hasattr(os, "getloadavg") else ()}
            except Exception:
                return {}


def _coerce_number(value: object) -> object:
    text = str(value)
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text
