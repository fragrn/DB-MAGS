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
        if kind == "hold_metadata_lock":
            return self._run_hold_metadata_lock(step)
        if kind == "shell":
            return self._run_shell(step)
        if kind == "workload_profile":
            return self._run_workload_profile(step)
        return {"executed": False, "error": f"unsupported step kind: {kind}"}

    @staticmethod
    def _run_sql(step: Dict[str, object]) -> Dict[str, object]:
        sql = str(step.get("sql", ""))
        database = step.get("database")
        started = time.perf_counter()
        try:
            with db_cursor(database=str(database) if database else None) as (conn, cur):
                cur.execute(sql)
                conn.commit()
            elapsed_seconds = time.perf_counter() - started
            latency_ms = elapsed_seconds * 1000.0
            return {
                "executed": True,
                "sql": sql,
                "database": database,
                "elapsed_seconds": elapsed_seconds,
                "latency_ms": latency_ms,
                "single_sql_mean_ms": latency_ms,
            }
        except Exception as exc:
            return {
                "executed": False,
                "sql": sql,
                "database": database,
                "error": str(exc),
                "elapsed_seconds": time.perf_counter() - started,
            }

    @staticmethod
    def _run_hold_sql(step: Dict[str, object]) -> Dict[str, object]:
        sql = str(step.get("sql", ""))
        database = step.get("database")
        hold_seconds = float(step.get("hold_seconds", 5))
        started = time.perf_counter()
        try:
            with db_cursor(database=str(database) if database else None) as (conn, cur):
                cur.execute(sql)
                time.sleep(hold_seconds)
                if sql.strip().lower().startswith("lock tables"):
                    cur.execute("UNLOCK TABLES")
                else:
                    conn.commit()
            elapsed_seconds = time.perf_counter() - started
            return {
                "executed": True,
                "sql": sql,
                "database": database,
                "hold_seconds": hold_seconds,
                "elapsed_seconds": elapsed_seconds,
                "latency_ms": elapsed_seconds * 1000.0,
            }
        except Exception as exc:
            return {
                "executed": False,
                "sql": sql,
                "database": database,
                "hold_seconds": hold_seconds,
                "error": str(exc),
                "elapsed_seconds": time.perf_counter() - started,
            }


    @staticmethod
    def _run_hold_metadata_lock(step: Dict[str, object]) -> Dict[str, object]:
        database = step.get("database")
        hold_seconds = float(step.get("hold_seconds", 5))
        sql = str(step.get("sql", "SELECT 1"))
        started = time.perf_counter()
        try:
            with db_cursor(database=str(database) if database else None) as (conn, cur):
                cur.execute("SET autocommit = 0")
                cur.execute("START TRANSACTION")
                cur.execute(sql)
                time.sleep(max(hold_seconds, 0.0))
                conn.commit()
            elapsed = time.perf_counter() - started
            return {
                "executed": True,
                "sql": sql,
                "database": database,
                "hold_seconds": hold_seconds,
                "elapsed_seconds": elapsed,
                "latency_ms": elapsed * 1000.0,
                "single_sql_mean_ms": elapsed * 1000.0,
            }
        except Exception as exc:
            return {"executed": False, "sql": sql, "database": database, "error": str(exc)}

    @staticmethod
    def _run_shell(step: Dict[str, object]) -> Dict[str, object]:
        command = str(step.get("command", ""))
        timeout_seconds = float(step.get("timeout_seconds", 20))
        started = time.perf_counter()
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return {
                "executed": False,
                "command": command,
                "stdout": (exc.stdout or "").strip(),
                "stderr": (exc.stderr or "").strip(),
                "returncode": None,
                "uid": "",
                "elapsed_seconds": time.perf_counter() - started,
                "error": f"command timed out after {timeout_seconds} seconds",
            }
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
            "elapsed_seconds": time.perf_counter() - started,
        }

    @staticmethod
    def _run_workload_profile(step: Dict[str, object]) -> Dict[str, object]:
        database = step.get("database")
        duration_seconds = float(step.get("duration_seconds", 10))
        deadline = time.time() + duration_seconds
        success = 0
        failures = 0
        sql = str(step.get("sql", "")).strip()
        latencies_ms = []
        started = time.perf_counter()
        while time.time() < deadline:
            loop_started = time.perf_counter()
            try:
                with db_cursor(database=str(database) if database else None) as (conn, _cur):
                    if sql:
                        _cur.execute(sql)
                    else:
                        txn, params = doOne()
                        executeTransaction(txn, params, conn)
                    conn.commit()
                success += 1
            except Exception:
                failures += 1
            finally:
                latencies_ms.append((time.perf_counter() - loop_started) * 1000.0)
            time.sleep(float(step.get("sleep_time", 0.001)))
        elapsed_seconds = max(time.perf_counter() - started, 1e-9)
        sorted_latencies = sorted(latencies_ms)
        avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0

        def percentile(fraction: float) -> float:
            if not sorted_latencies:
                return 0.0
            index = min(max(int(round((len(sorted_latencies) - 1) * fraction)), 0), len(sorted_latencies) - 1)
            return sorted_latencies[index]

        return {
            "executed": success > 0,
            "database": database,
            "duration_seconds": duration_seconds,
            "elapsed_seconds": elapsed_seconds,
            "successful_transactions": success,
            "failed_transactions": failures,
            "qps": success / elapsed_seconds,
            "avg_latency_ms": avg_latency,
            "p95_latency_ms": percentile(0.95),
            "p99_latency_ms": percentile(0.99),
            "single_sql_mean_ms": avg_latency if sql else 0.0,
            "thread_count": int(step.get("thread_count", 1)),
            "sql": sql,
        }
