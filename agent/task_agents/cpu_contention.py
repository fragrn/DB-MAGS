import os
from typing import List
from uuid import uuid4

from agent.models import DatabaseProfile, TaskSpec
from agent.task_agents.base import BaseTaskAgent


class CpuContentionAgent(BaseTaskAgent):
    name = "cpu_contention"

    def plan(self, profile: DatabaseProfile, runtime_context: dict) -> List[TaskSpec]:
        blade_path = self._resolve_blade_path(runtime_context)
        start_after = int(runtime_context.get("fault_inject_time", 60))
        duration = int(runtime_context.get("fault_duration", 60))
        cpu_count = int(runtime_context.get("cpu_core_count", 1))
        load = int(runtime_context.get("cpu_load", 95))
        task_id = f"cpu-{uuid4().hex[:8]}"
        return [
            TaskSpec(
                task_id=task_id,
                task_type="cpu_contention",
                agent_name=self.name,
                start_after_seconds=start_after,
                duration_seconds=duration,
                payload={
                    "mode": "command",
                    "blade_path": blade_path,
                    "create_command": f'{blade_path} create cpu fullload --cpu-count {cpu_count} --cpu-percent {load}',
                    "precheck_command": f'{blade_path} version',
                },
                cleanup_payload={
                    "mode": "chaosblade_destroy",
                    "blade_path": blade_path,
                },
                metadata={
                    "description": "Inject host-level CPU contention with ChaosBlade.",
                    "schema": profile.schema_name,
                },
            )
        ]

    def _resolve_blade_path(self, runtime_context: dict) -> str:
        configured = runtime_context.get("chaosblade_path") or os.getenv("DBMAGS_CHAOSBLADE_PATH")
        if configured:
            return configured

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        candidates = [
            os.path.join(repo_root, ".tools", "chaosblade", "blade"),
            os.path.join(repo_root, ".tools", "chaosblade-1.8.0-darwin_arm64", "blade"),
            "/opt/chaosblade/blade",
            "/usr/local/bin/blade",
            "/opt/homebrew/bin/blade",
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return os.path.join(repo_root, ".tools", "chaosblade", "blade")
