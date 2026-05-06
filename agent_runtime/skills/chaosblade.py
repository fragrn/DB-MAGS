from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict

from agent_runtime.skills.base import Skill
from tpcc_operation_set import cpu_bottle, disk_bottle, io_bottle, mem_bottle, net_bottle


class ChaosBladeInjectionSkill(Skill):
    name = "chaosblade_injection_skill"
    _DEFAULT_LEGACY_PATH = "/root/ChaosBlade/chaosblade-0.3.0/blade"
    _DEFAULT_REPO_PATH = ".tools/chaosblade-1.8.0-darwin_arm64/blade"

    def execute(self, resource_type: str, execute: bool = False) -> Dict[str, object]:
        command = self._select_command(resource_type)
        result = {"command": command, "executed": False, "uid": "", "blade_path": self._resolve_blade_path()}
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

    @classmethod
    def cleanup(cls, uid: str) -> Dict[str, object]:
        if not uid:
            return {"cleaned": False, "error": "missing chaosblade uid"}
        command = f"{cls._resolve_blade_path()} destroy {uid}"
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {"cleaned": proc.returncode == 0, "stdout": (proc.stdout or proc.stderr).strip(), "command": command}

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

    @classmethod
    def _resolve_blade_path(cls) -> str:
        candidates = []
        env_path = os.getenv("DBMAGS_CHAOSBLADE_PATH", "").strip()
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path.cwd() / cls._DEFAULT_REPO_PATH)
        candidates.append(Path(cls._DEFAULT_LEGACY_PATH))
        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return env_path or str(Path.cwd() / cls._DEFAULT_REPO_PATH)

    @classmethod
    def _select_command(cls, resource_type: str) -> str:
        blade = cls._resolve_blade_path()
        if resource_type == "network":
            return f"{blade} create network drop --destination-port 3306 --network-traffic out"
        if resource_type == "disk":
            return f"{blade} create disk fill --path /tmp --size 64"
        mapping = {
            "cpu": cpu_bottle,
            "io": io_bottle,
            "disk": disk_bottle,
            "memory": mem_bottle,
            "network": net_bottle,
        }
        generator = mapping.get(resource_type, cpu_bottle)
        command = generator()[0]
        return command.replace(cls._DEFAULT_LEGACY_PATH, blade)
