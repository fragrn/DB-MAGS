from __future__ import annotations

import subprocess
from typing import Dict

from agent_runtime.db import db_cursor
from agent_runtime.skills.base import Skill


class RunInjectionSkill(Skill):
    name = "run_injection_skill"

    def execute(self, step: Dict[str, object]) -> Dict[str, object]:
        kind = step.get("kind")
        if kind == "sql":
            return self._run_sql(step)
        if kind == "shell":
            return self._run_shell(step)
        if kind == "workload_profile":
            return {"executed": True, "details": step}
        return {"executed": False, "error": f"unsupported step kind: {kind}"}

    @staticmethod
    def _run_sql(step: Dict[str, object]) -> Dict[str, object]:
        sql = str(step.get("sql", ""))
        try:
            with db_cursor() as (conn, cur):
                cur.execute(sql)
                conn.commit()
            return {"executed": True, "sql": sql}
        except Exception as exc:
            return {"executed": False, "sql": sql, "error": str(exc)}

    @staticmethod
    def _run_shell(step: Dict[str, object]) -> Dict[str, object]:
        command = str(step.get("command", ""))
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {
            "executed": proc.returncode == 0,
            "command": command,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "returncode": proc.returncode,
        }
