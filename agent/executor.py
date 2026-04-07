import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.models import RunReport, TaskResult, TaskSpec


class TaskExecutor:
    def __init__(self, output_dir: str, database=None, max_workers: Optional[int] = None, runtime_metadata: Optional[Dict] = None):
        self.output_dir = Path(output_dir)
        self.database = database or self._default_database()
        self.max_workers = max_workers or 4
        self.runtime_metadata = runtime_metadata or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def execute_plan(self, plan: List[TaskSpec]) -> RunReport:
        results: List[TaskResult] = []
        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(plan) or 1))) as pool:
            futures = {pool.submit(self._run_task, task): task for task in plan}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item.task_id)
        report = RunReport(plan=plan, results=results, output_dir=str(self.output_dir), runtime=self.runtime_metadata)
        self._write_json("plan.json", [asdict(task) for task in plan])
        self._write_json("results.json", report.to_dict())
        return report

    def _run_task(self, task: TaskSpec) -> TaskResult:
        started_at = datetime.utcnow().isoformat()
        log_path = self.output_dir / f"{task.task_id}.log"
        result = TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            agent_name=task.agent_name,
            status="running",
            started_at=started_at,
            metadata=task.metadata,
            artifacts={"log": str(log_path)},
        )
        try:
            self._log(log_path, f"Task {task.task_id} waiting {task.start_after_seconds}s before start")
            time.sleep(max(0, task.start_after_seconds))
            if task.payload.get("mode") == "command":
                self._execute_command_task(task, log_path, result)
            elif task.payload.get("mode") == "sql":
                self._execute_sql_task(task, log_path, result)
            else:
                raise ValueError(f"Unsupported task mode: {task.payload.get('mode')}")
            result.status = "success"
        except Exception as exc:
            result.status = "failed"
            result.error = str(exc)
            self._log(log_path, f"Task failed: {exc}")
        finally:
            result.finished_at = datetime.utcnow().isoformat()
        return result

    def _execute_command_task(self, task: TaskSpec, log_path: Path, result: TaskResult) -> None:
        precheck = task.payload.get("precheck_command")
        if precheck:
            self._run_command(precheck, log_path)
        completed = self._run_command(task.payload["create_command"], log_path)
        uid = self._extract_chaosblade_uid(completed.stdout)
        if uid:
            result.artifacts["chaosblade_uid"] = uid
        time.sleep(max(0, task.duration_seconds))
        cleanup_payload = dict(task.cleanup_payload)
        if uid:
            cleanup_payload["uid"] = uid
        self._cleanup(cleanup_payload, log_path)

    def _execute_sql_task(self, task: TaskSpec, log_path: Path, result: TaskResult) -> None:
        repeat = int(task.payload.get("repeat", 1))
        sleep_seconds = float(task.payload.get("sleep_seconds", 0))
        sql = task.payload["sql"]
        conn, cur = self.database.connection2()
        try:
            for _ in range(repeat):
                cur.execute(sql)
                cur.fetchall()
                self._log(log_path, f"Executed SQL: {sql}")
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            conn.commit()
            result.artifacts["sql"] = sql
        finally:
            conn.close()

    def _cleanup(self, cleanup_payload: dict, log_path: Path) -> None:
        if not cleanup_payload:
            return
        if cleanup_payload.get("mode") != "chaosblade_destroy":
            return
        uid = cleanup_payload.get("uid")
        blade_path = cleanup_payload.get("blade_path")
        if not uid or not blade_path:
            self._log(log_path, "Skipping cleanup because ChaosBlade uid was not available")
            return
        self._run_command(f"{blade_path} destroy {uid}", log_path, check=False)

    def _run_command(self, command: str, log_path: Path, check: bool = True) -> subprocess.CompletedProcess:
        self._log(log_path, f"Running command: {command}")
        completed = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.stdout:
            self._log(log_path, completed.stdout.strip())
        if completed.stderr:
            self._log(log_path, completed.stderr.strip())
        if check and completed.returncode != 0:
            raise RuntimeError(f"Command failed ({completed.returncode}): {command}")
        return completed

    def _extract_chaosblade_uid(self, stdout: str) -> Optional[str]:
        if not stdout:
            return None
        for line in stdout.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                if isinstance(data.get("result"), str):
                    return data["result"]
                if isinstance(data.get("result"), dict) and data["result"].get("uid"):
                    return data["result"]["uid"]
        return None

    def _write_json(self, file_name: str, payload) -> None:
        path = self.output_dir / file_name
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

    def _log(self, path: Path, message: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.utcnow().isoformat()}] {message}\n")

    @staticmethod
    def _default_database():
        from Connection.Connection import Database

        return Database()
