"""
Task DAG executor: runs an ExecutableTaskDAG using a ThreadPoolExecutor,
dispatching actions by kind (sql_workload, workload_ramp, lock_conflict,
chaosblade, logical_backup), tracking per-task start/end times, and
collecting cleanup actions at the end.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from agent.config import RuntimeConfig
from agent.types import ExecutionTrace, TaskResult
from agent.tools import benchbase_transaction_types, validate_traffic_surge_profile


class Executor:
    """Execute an ExecutableTaskDAG dict and return an ExecutionTrace."""

    def __init__(self, config: RuntimeConfig, round_dir: str | None = None):
        self.config = config
        self.round_dir = Path(round_dir) if round_dir else Path.cwd()
        self._chaosblade_uids: list[str] = []
        self._lock = threading.Lock()

    def execute(
        self,
        task_dag: dict[str, Any],
        max_duration_sec: int = 300,
    ) -> ExecutionTrace:
        """
        Execute all tasks in topological order, respecting dependencies and
        start_after_sec offsets.
        """
        tasks = task_dag.get("tasks", {})
        edges = task_dag.get("edges", [])
        schedule = task_dag.get("schedule", {})

        trace = ExecutionTrace()
        for task_id in tasks:
            trace.tasks[task_id] = TaskResult(task_id=task_id)

        # Build adjacency list for dependency checking
        successors_map: dict[str, list[str]] = {t: [] for t in tasks}
        for e in edges:
            successors_map.setdefault(e.source, []).append(e.target)

        def wait_for_dependencies(
            completed: dict[str, TaskResult],
            deps: list[str],
        ) -> None:
            """Spin until all dependency tasks have started."""
            while True:
                all_done = all(
                    completed[d].status in ("completed", "failed", "skipped")
                    for d in deps
                )
                if all_done:
                    break
                time.sleep(0.2)

        def run_task(task_id: str, spec: dict, scheduled_offset: float) -> None:
            tr = trace.tasks[task_id]

            # Wait for dependencies
            deps = [e["source"] for e in edges if e["target"] == task_id]
            wait_for_dependencies(trace.tasks, deps)

            # Wait for scheduled offset
            offset = scheduled_offset + spec.get("start_after_sec", 0.0)
            if offset > 0:
                time.sleep(offset)

            # Mark start
            tr.status = "running"
            tr.start_time = _now_iso()

            try:
                for action in spec.get("actions", []):
                    kind = action.get("kind", "")
                    try:
                        result = self._run_action(kind, action, task_id)
                        tr.stdout += str(result) + "\n"
                    except Exception as exc:
                        tr.stderr += str(exc) + "\n"
                        tr.errors.append(str(exc))
                tr.status = "completed" if not tr.errors else "failed"
            except Exception as exc:
                tr.status = "failed"
                tr.errors.append(str(exc))
                tr.stderr += str(exc) + "\n"
            finally:
                tr.end_time = _now_iso()

        # Submit all tasks that have no dependencies first
        no_dep = {tid for tid in tasks if not any(e["target"] == tid for e in edges)}

        with ThreadPoolExecutor(max_workers=min(len(tasks), 20)) as pool:
            futures = {}
            for task_id in no_dep:
                spec = tasks[task_id]
                offset = schedule.get(task_id, 0.0) + spec.get("start_after_sec", 0.0)
                f = pool.submit(run_task, task_id, spec, offset)
                futures[f] = task_id

            # Wait for completion
            for f in as_completed(futures, timeout=max_duration_sec + 30):
                task_id = futures[f]
                try:
                    f.result()
                except Exception as exc:
                    trace.tasks[task_id].status = "failed"
                    trace.tasks[task_id].errors.append(str(exc))

        # Run cleanup actions for tasks that completed
        self._run_cleanup(task_dag, trace)

        return trace

    # -------------------------------------------------------------------------
    # Action dispatch
    # -------------------------------------------------------------------------

    def _run_action(self, kind: str, action: dict, task_id: str) -> Any:
        if kind == "sql_workload":
            return self._run_sql_workload(action)
        elif kind == "workload_ramp":
            return self._run_workload_ramp(action)
        elif kind == "benchbase_burst":
            return self._run_benchbase_burst(action, task_id)
        elif kind == "lock_conflict":
            return self._run_lock_conflict(action, task_id)
        elif kind == "chaosblade":
            return self._run_chaosblade(action, task_id)
        elif kind == "logical_backup":
            return self._run_backup(action)
        else:
            return {"error": f"Unknown action kind: {kind}"}

    def _run_sql_workload(self, action: dict) -> dict:
        """Run a SQL workload in multiple threads."""
        import pymysql

        sql = action.get("sql", "SELECT 1")
        concurrency = action.get("concurrency", 4)
        duration_sec = action.get("duration_sec", 30)
        database = action.get("database", self.config.default_database)
        results: list[float] = []
        errors: list[str] = []
        stop_flag = threading.Event()

        def worker() -> None:
            conn = pymysql.connect(
                host=self.config.mysql_host,
                port=self.config.mysql_port,
                user=self.config.mysql_user,
                password=self.config.mysql_password,
                database=database,
            )
            cur = conn.cursor()
            deadline = time.time() + duration_sec
            while not stop_flag.is_set() and time.time() < deadline:
                try:
                    t0 = time.perf_counter()
                    cur.execute(sql)
                    cur.fetchall()
                    results.append((time.perf_counter() - t0) * 1000)
                except Exception as exc:
                    errors.append(str(exc))
                time.sleep(0.01)
            cur.close()
            conn.close()

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        deadline = time.time() + duration_sec + 5
        for t in threads:
            t.join(timeout=max(0, deadline - time.time()))
        stop_flag.set()

        if results:
            results.sort()
            n = len(results)
            return {
                "concurrency": concurrency,
                "duration_sec": duration_sec,
                "executions": len(results),
                "latency_ms": {
                    "min": round(results[0], 3),
                    "p50": round(results[n // 2], 3),
                    "p95": round(results[int(n * 0.95)], 3),
                    "max": round(results[-1], 3),
                },
                "error_count": len(errors),
            }
        return {"error_count": len(errors), "errors": errors[:5]}

    def _run_workload_ramp(self, action: dict) -> dict:
        """Gradually increase concurrency in stages."""
        database = action.get("database", self.config.default_database)
        stages = action.get("ramp_stages", [{"at_sec": 0, "connections": 10}])
        duration_sec = action.get("duration_sec", 60)
        sql = action.get("sql", "SELECT 1")
        results = {}

        for stage in stages:
            at_sec = stage.get("at_sec", 0)
            connections = stage.get("connections", 10)
            time.sleep(at_sec - sum(s.get("at_sec", 0) for s in stages[:stages.index(stage)]))
            import pymysql
            conns = []
            for _ in range(connections):
                c = pymysql.connect(
                    host=self.config.mysql_host, port=self.config.mysql_port,
                    user=self.config.mysql_user, password=self.config.mysql_password,
                    database=database,
                )
                conns.append(c)
            results[f"stage_{at_sec}s"] = {"connections": connections, "connected": True}
            time.sleep(min(5, duration_sec - at_sec))

        time.sleep(max(0, duration_sec - sum(s.get("at_sec", 0) + 5 for s in stages)))
        return results

    def _run_benchbase_burst(self, action: dict, task_id: str) -> dict:
        """Run an additional BenchBase client using a validated burst profile."""
        profile = validate_traffic_surge_profile(action.get("profile") or {})
        self.round_dir.mkdir(parents=True, exist_ok=True)
        config_path = self._materialize_benchbase_burst_config(profile, task_id)
        jar_path = _resolve_path(str(action.get("jar_path") or ".tools/benchbase-main/target/benchbase-mysql/benchbase.jar"))
        java_bin = str(action.get("java_bin") or "/opt/homebrew/opt/openjdk/bin/java")
        command = [
            java_bin,
            "-jar",
            str(jar_path),
            "-b",
            profile["benchmark"],
            "-c",
            str(config_path),
            "--create=false",
            "--load=false",
            "--execute=true",
        ]
        stdout_path = self.round_dir / f"{task_id}_benchbase_burst_stdout.log"
        stderr_path = self.round_dir / f"{task_id}_benchbase_burst_stderr.log"
        timeout_sec = float(profile["duration_sec"]) + 30.0
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                command,
                cwd=str(jar_path.parent),
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            try:
                exit_code = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                raise RuntimeError(f"BenchBase burst timed out after {timeout_sec:.1f}s")
        result = {
            "kind": "benchbase_burst",
            "pid": proc.pid,
            "exit_code": exit_code,
            "command": command,
            "runtime_config_path": str(config_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": _tail(stdout_path),
            "stderr_tail": _tail(stderr_path),
            "profile": profile,
        }
        if exit_code != 0:
            raise RuntimeError(f"BenchBase burst exited with {exit_code}: {_tail(stderr_path)}")
        return result

    def _materialize_benchbase_burst_config(self, profile: dict[str, Any], task_id: str) -> Path:
        source = _resolve_path(str(profile["config_path"]))
        if not source.exists():
            raise FileNotFoundError(f"BenchBase burst config not found: {source}")
        tree = ET.parse(source)
        root = tree.getroot()
        _set_text(root, ".//work/time", str(int(float(profile["duration_sec"]))))
        _set_text(root, "terminals", str(int(profile["terminals"])))
        rate = str(profile["rate"])
        _set_text(root, ".//works/work/rate", rate)
        _set_text(root, ".//work/rate", rate)
        transaction_types = benchbase_transaction_types(profile["benchmark"], str(source))
        weights = ",".join(str(_format_weight(profile["transaction_mix"].get(name, 0))) for name in transaction_types)
        _set_text(root, ".//work/weights", weights)
        _replace_jdbc_database(root, profile["database"])
        out = self.round_dir / f"{task_id}_benchbase_burst_config.xml"
        tree.write(out, encoding="utf-8", xml_declaration=True)
        return out

    def _run_lock_conflict(self, action: dict, task_id: str) -> dict:
        """Run holder/waiter lock conflict pattern."""
        database = action.get("database", self.config.default_database)
        table = action.get("table", "")
        key_column = action.get("key_column", "")
        holder_conc = action.get("holder_concurrency", 1)
        waiter_conc = action.get("waiter_concurrency", 4)
        hold_sec = action.get("hold_sec", 10.0)
        lock_type = action.get("lock_type", "record_lock")

        import pymysql

        holder_errors: list[str] = []
        waiter_errors: list[str] = []

        def holder():
            try:
                conn = pymysql.connect(
                    host=self.config.mysql_host, port=self.config.mysql_port,
                    user=self.config.mysql_user, password=self.config.mysql_password,
                    database=database,
                )
                cur = conn.cursor()
                if lock_type == "record_lock":
                    cur.execute(f"SELECT * FROM {table} LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        key_val = row[0]
                        cur.execute(
                            f"SELECT * FROM {table} WHERE {key_column}=%s FOR UPDATE",
                            (key_val,),
                        )
                        time.sleep(hold_sec)
                conn.commit()
                cur.close()
                conn.close()
            except Exception as exc:
                holder_errors.append(str(exc))

        def waiter():
            try:
                time.sleep(0.5)  # let holder acquire lock first
                conn = pymysql.connect(
                    host=self.config.mysql_host, port=self.config.mysql_port,
                    user=self.config.mysql_user, password=self.config.mysql_password,
                    database=database,
                    connect_timeout=5,
                )
                cur = conn.cursor()
                if lock_type == "record_lock":
                    cur.execute(f"SELECT * FROM {table} LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        key_val = row[0]
                        cur.execute(
                            f"UPDATE {table} SET {key_column}=%s WHERE {key_column}=%s",
                            (key_val, key_val),
                        )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as exc:
                waiter_errors.append(str(exc))

        threads = (
            [threading.Thread(target=holder) for _ in range(holder_conc)]
            + [threading.Thread(target=waiter) for _ in range(waiter_conc)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=hold_sec + 10)

        return {
            "holder_errors": holder_errors,
            "waiter_errors": waiter_errors,
        }

    def _run_chaosblade(self, action: dict, task_id: str) -> dict:
        """Run a ChaosBlade command and track the UID for cleanup."""
        blade_path = action.get("chaosblade_path", self.config.chaosblade_path)
        resource = action.get("resource_type", "cpu")
        duration = action.get("duration_sec", 30)
        intensity = action.get("intensity", "high")

        uid = f"{task_id}_{uuid.uuid4().hex[:6]}"

        cmd_map = {
            "cpu": [blade_path, "create", "cpu", "load", "--cpu-percent", "90"],
            "io": [blade_path, "create", "disk", "fill", "--path", "/tmp", "--size", "100M"],
            "memory": [blade_path, "create", "mem", "load", "--mem-percent", "80"],
            "network": [blade_path, "create", "network", "delay", "--local-port", "3306", "--time", "300"],
        }
        cmd = cmd_map.get(resource, cmd_map["cpu"])
        cmd.extend(["--uid", uid, "--timeout", f"{duration}s"])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=duration + 10,
            )
            with self._lock:
                self._chaosblade_uids.append(uid)
            return {"uid": uid, "returncode": result.returncode, "stdout": result.stdout[:500]}
        except Exception as exc:
            return {"uid": uid, "error": str(exc)}

    def _run_backup(self, action: dict) -> dict:
        """Run mysqldump on a table."""
        database = action.get("database", self.config.default_database)
        table = action.get("table", "")
        tool = action.get("tool", "mysqldump")

        if tool == "mysqldump":
            cmd = [
                "mysqldump",
                f"--host={self.config.mysql_host}",
                f"--port={self.config.mysql_port}",
                f"--user={self.config.mysql_user}",
                f"--password={self.config.mysql_password}",
                "--single-transaction",
                database, table,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                )
                return {"returncode": result.returncode, "stdout_len": len(result.stdout)}
            except Exception as exc:
                return {"error": str(exc)}
        return {"error": f"Unknown backup tool: {tool}"}

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def _run_cleanup(self, task_dag: dict, trace: ExecutionTrace) -> None:
        """Destroy ChaosBlade UIDs created during execution."""
        for uid in self._chaosblade_uids:
            try:
                blade_path = self.config.chaosblade_path
                subprocess.run(
                    [blade_path, "destroy", uid],
                    capture_output=True, timeout=10,
                )
            except Exception as exc:
                trace.cleanup_errors.append(f"Failed to destroy {uid}: {exc}")
        self._chaosblade_uids.clear()
        trace.cleanup_status = "completed" if not trace.cleanup_errors else "partial_failure"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_path(path: str) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return (Path.cwd() / p).resolve()


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-limit:]
    except Exception:
        return ""


def _format_weight(value: Any) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:g}"


def _set_text(root: ET.Element, path: str, value: str) -> None:
    elems = root.findall(path)
    if elems:
        for elem in elems:
            elem.text = value
        return
    if path == "terminals":
        ET.SubElement(root, "terminals").text = value
        return
    if path.endswith("/rate") or path.endswith("/weights") or path.endswith("/time"):
        work = root.find(".//work")
        if work is not None:
            ET.SubElement(work, path.rsplit("/", 1)[-1]).text = value


def _replace_jdbc_database(root: ET.Element, database: str) -> None:
    for elem in root.iter():
        if elem.text and "jdbc:mysql://" in elem.text:
            elem.text = re.sub(
                r"(jdbc:mysql://[^/]+/)([^?]+)",
                rf"\g<1>{database}",
                elem.text,
                count=1,
            )
