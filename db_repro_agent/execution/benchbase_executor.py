"""BenchBase workload executor."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from ..models.action import ActionResult, BenchBaseAction
from .command_executor import CommandExecutor, _iso


class BenchBaseExecutor:
    def __init__(self, command_executor: CommandExecutor | None = None) -> None:
        self.command_executor = command_executor or CommandExecutor()
        self.process: subprocess.Popen | None = None
        self.action: BenchBaseAction | None = None

    def validate(self, action: BenchBaseAction) -> None:
        if not os.path.isfile(action.jar_path):
            raise ValueError(f"BenchBase jar does not exist: {action.jar_path}")
        if not os.path.isfile(action.config_path):
            raise ValueError(f"BenchBase config does not exist: {action.config_path}")
        if action.benchmark != action.benchmark.strip():
            raise ValueError("benchmark must not contain surrounding whitespace")

    def start(self, action: BenchBaseAction) -> ActionResult:
        action = self._normalize_action(action)
        self.validate(action)
        command = self._command(action)
        started = time.time()
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(Path(action.jar_path).parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.action = action
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=True,
                status="started",
                started_at=_iso(started),
                finished_at=_iso(time.time()),
                duration_ms=round((time.time() - started) * 1000, 3),
                metadata={"pid": self.process.pid, "command": command},
            )
        except OSError as exc:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=False,
                status="failed",
                started_at=_iso(started),
                finished_at=_iso(time.time()),
                duration_ms=round((time.time() - started) * 1000, 3),
                error_code=getattr(exc, "errno", None) or type(exc).__name__,
                error_message=str(exc),
            )

    def execute(self, action: BenchBaseAction) -> ActionResult:
        action = self._normalize_action(action)
        if self.process is None or self.process.poll() is not None:
            started = self.start(action)
            if not started.success:
                return started
        assert self.process is not None
        try:
            stdout, stderr = self.process.communicate(timeout=action.duration_sec + 30)
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=self.process.returncode == 0,
                status="succeeded" if self.process.returncode == 0 else "failed",
                started_at=_iso(time.time()),
                finished_at=_iso(time.time()),
                duration_ms=0,
                exit_code=self.process.returncode,
                stdout=stdout,
                stderr=stderr,
                error_code=None if self.process.returncode == 0 else self.process.returncode,
                error_message=None if self.process.returncode == 0 else "BenchBase exited with non-zero status",
                metadata={"pid": self.process.pid, "command": self._command(action)},
            )
        except subprocess.TimeoutExpired:
            self.stop(action)
            return ActionResult(
                action_id=action.action_id,
                action_type=action.type,
                success=False,
                status="timed_out",
                started_at=_iso(time.time()),
                finished_at=_iso(time.time()),
                duration_ms=0,
                error_code="TIMEOUT",
                error_message="BenchBase exceeded its action timeout",
                metadata={"pid": self.process.pid, "command": self._command(action)},
            )

    def stop(self, action: BenchBaseAction) -> ActionResult:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        return _lifecycle_result(action, "stopped")

    def cleanup(self, action: BenchBaseAction) -> ActionResult:
        self.stop(action)
        self.process = None
        self.action = None
        return _lifecycle_result(action, "cleaned")

    def is_alive(self) -> bool:
        """Return whether the background BenchBase process is alive."""
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def _normalize_action(action: BenchBaseAction) -> BenchBaseAction:
        jar_path = str(Path(action.jar_path).expanduser().resolve())
        return action.model_copy(update={"jar_path": jar_path})

    @staticmethod
    def _command(action: BenchBaseAction) -> list[str]:
        command = [action.java_bin, "-jar", action.jar_path, "-b", action.benchmark, "-c", action.config_path, "--execute=true"]
        command.extend(["--directory", action.results_dir])
        return command


def _lifecycle_result(action: BenchBaseAction, status: str) -> ActionResult:
    now = _iso(time.time())
    return ActionResult(action_id=action.action_id, action_type=action.type, success=True, status=status, started_at=now, finished_at=now, duration_ms=0)
