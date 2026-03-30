from __future__ import annotations

import json
import subprocess
from typing import Dict

from agent_runtime.skills.base import Skill
from tpcc_operation_set import cpu_bottle, disk_bottle, io_bottle, mem_bottle, net_bottle


class ChaosBladeInjectionSkill(Skill):
    name = "chaosblade_injection_skill"

    def execute(self, resource_type: str, execute: bool = False) -> Dict[str, object]:
        command = self._select_command(resource_type)
        result = {"command": command, "executed": False, "uid": ""}
        if not execute:
            return result
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        stdout = (proc.stdout or "") + (proc.stderr or "")
        result["executed"] = proc.returncode == 0
        result["stdout"] = stdout.strip()
        uid = self._extract_uid(stdout)
        if uid:
            result["uid"] = uid
        return result

    @staticmethod
    def cleanup(uid: str) -> Dict[str, object]:
        if not uid:
            return {"cleaned": False, "error": "missing chaosblade uid"}
        command = f"/root/ChaosBlade/chaosblade-0.3.0/blade destroy {uid}"
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {"cleaned": proc.returncode == 0, "stdout": (proc.stdout or proc.stderr).strip()}

    @staticmethod
    def _extract_uid(stdout: str) -> str:
        try:
            payload = json.loads(stdout)
            return payload.get("result", "") or payload.get("uid", "")
        except Exception:
            pass
        for token in stdout.replace("\n", " ").split():
            if token.startswith("uid="):
                return token.split("=", 1)[1]
        return ""

    @staticmethod
    def _select_command(resource_type: str) -> str:
        mapping = {
            "cpu": cpu_bottle,
            "io": io_bottle,
            "disk": disk_bottle,
            "memory": mem_bottle,
            "network": net_bottle,
        }
        generator = mapping.get(resource_type, cpu_bottle)
        return generator()[0]
