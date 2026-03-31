from __future__ import annotations

import json
import subprocess
import time
from typing import Dict

from agent_runtime.db import db_cursor
from agent_runtime.skills.base import Skill
from Tpcc.tpcc import doOne, executeTransaction


class RunInjectionSkill(Skill):
    name = "run_injection_skill"

    def execute(self, step: Dict[str, object]) -> Dict[str, object]:
        kind = step.get("kind")
        if kind == "sql":
            return self._run_sql(step)
        if kind == "hold_sql":
            return self._run_hold_sql(step)
        if kind == "shell":
            return self._run_shell(step)
        if kind == "workload_profile":
            return self._run_workload_profile(step)
        return {"executed": False, "error": f"unsupported step kind: {kind}"}

    @staticmethod
    def _run_sql(step: Dict[str, object]) -> Dict[str, object]:
        sql = str(step.get("sql", ""))
        database = step.get("database")
        try:
            with db_cursor(database=str(database) if database else None) as (conn, cur):
                cur.execute(sql)
                conn.commit()
            return {"executed": True, "sql": sql, "database": database}
        except Exception as exc:
            return {"executed": False, "sql": sql, "database": database, "error": str(exc)}

    @staticmethod
    def _run_hold_sql(step: Dict[str, object]) -> Dict[str, object]:
        sql = str(step.get("sql", ""))
        database = step.get("database")
        hold_seconds = float(step.get("hold_seconds", 5))
        try:
            with db_cursor(database=str(database) if database else None) as (conn, cur):
                cur.execute(sql)
                time.sleep(hold_seconds)
                if sql.strip().lower().startswith("lock tables"):
                    cur.execute("UNLOCK TABLES")
                else:
                    conn.commit()
            return {"executed": True, "sql": sql, "database": database, "hold_seconds": hold_seconds}
        except Exception as exc:
            return {"executed": False, "sql": sql, "database": database, "hold_seconds": hold_seconds, "error": str(exc)}

    @staticmethod
    def _run_shell(step: Dict[str, object]) -> Dict[str, object]:
        command = str(step.get("command", ""))
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        combined = stdout or stderr
        uid = ""
        try:
            payload = json.loads(combined) if combined else {}
            uid = payload.get("result", "") or payload.get("uid", "")
        except Exception:
            uid = ""
        return {
            "executed": proc.returncode == 0,
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
            "uid": uid,
        }

    @staticmethod
    def _run_workload_profile(step: Dict[str, object]) -> Dict[str, object]:
        database = step.get("database")
        duration_seconds = float(step.get("duration_seconds", 10))
        deadline = time.time() + duration_seconds
        success = 0
        failures = 0
        while time.time() < deadline:
            try:
                with db_cursor(database=str(database) if database else None) as (conn, _cur):
                    txn, params = doOne()
                    executeTransaction(txn, params, conn)
                    conn.commit()
                success += 1
            except Exception:
                failures += 1
            time.sleep(float(step.get("sleep_time", 0.001)))
        return {
            "executed": success > 0,
            "database": database,
            "duration_seconds": duration_seconds,
            "successful_transactions": success,
            "failed_transactions": failures,
            "thread_count": int(step.get("thread_count", 1)),
        }
