"""ChaosBlade executor with explicit UID cleanup."""

from __future__ import annotations

import re
import shlex
import subprocess
import time

from ..models.action import ActionResult, ChaosBladeAction
from .command_executor import CommandExecutor, _iso


class ChaosExecutor:
    def __init__(self, command_executor: CommandExecutor | None = None) -> None:
        self.command_executor = command_executor or CommandExecutor()
        self.active_uid: str | None = None

    def validate(self, action: ChaosBladeAction) -> None:
        if not action.command.strip():
            raise ValueError("ChaosBlade command must not be empty")
        if any(token in action.command for token in (";", "&&", "||", "|", "`", "$()")):
            raise ValueError("ChaosBlade command contains shell control syntax")

    def start(self, action: ChaosBladeAction) -> ActionResult:
        self.validate(action)
        started = time.time()
        command = [action.blade_path, "create", *shlex.split(action.command)]
        result = self.command_executor.run(
            action_id=action.action_id,
            action_type=action.type,
            command=command,
            timeout_sec=action.duration_sec + 10,
        )
        if result.success:
            self.active_uid = _extract_uid(result.stdout)
            result.metadata["uid"] = self.active_uid
        return result

    def execute(self, action: ChaosBladeAction) -> ActionResult:
        if self.active_uid is None:
            return self.start(action)
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="succeeded",
            started_at=_iso(time.time()),
            finished_at=_iso(time.time()),
            duration_ms=0,
            metadata={"uid": self.active_uid},
        )

    def stop(self, action: ChaosBladeAction) -> ActionResult:
        if self.active_uid is None:
            return _lifecycle_result(action, "stopped")
        result = self.command_executor.run(
            action_id=action.action_id,
            action_type=action.type,
            command=[action.blade_path, "destroy", self.active_uid],
            timeout_sec=15,
        )
        if result.success:
            self.active_uid = None
        return result

    def cleanup(self, action: ChaosBladeAction) -> ActionResult:
        result = self.stop(action)
        if result.success:
            result.status = "cleaned"
        return result


def _extract_uid(stdout: str) -> str | None:
    for pattern in (r'"uid"\s*:\s*"([A-Za-z0-9_-]+)"', r"uid[=: ]+([A-Za-z0-9_-]+)"):
        match = re.search(pattern, stdout, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _lifecycle_result(action: ChaosBladeAction, status: str) -> ActionResult:
    now = _iso(time.time())
    return ActionResult(action_id=action.action_id, action_type=action.type, success=True, status=status, started_at=now, finished_at=now, duration_ms=0)
