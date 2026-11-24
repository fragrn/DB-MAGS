"""Safe subprocess boundary for BenchBase and ChaosBlade actions."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

from ..models.action import ActionResult


class CommandExecutor:
    def run(
        self,
        *,
        action_id: str,
        action_type: str,
        command: Sequence[str],
        timeout_sec: float,
        cwd: str | Path | None = None,
    ) -> ActionResult:
        started = time.monotonic()
        wall_start = time.time()
        try:
            completed = subprocess.run(
                list(command),
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            return ActionResult(
                action_id=action_id,
                action_type=action_type,
                success=completed.returncode == 0,
                status="succeeded" if completed.returncode == 0 else "failed",
                started_at=_iso(wall_start),
                finished_at=_iso(time.time()),
                duration_ms=duration_ms,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                error_code=None if completed.returncode == 0 else completed.returncode,
                error_message=None if completed.returncode == 0 else "command exited with non-zero status",
                metadata={"command": list(command)},
            )
        except subprocess.TimeoutExpired as exc:
            return ActionResult(
                action_id=action_id,
                action_type=action_type,
                success=False,
                status="timed_out",
                started_at=_iso(wall_start),
                finished_at=_iso(time.time()),
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                error_code="TIMEOUT",
                error_message=f"command exceeded timeout of {timeout_sec} seconds",
                metadata={"command": list(command)},
            )
        except OSError as exc:
            return ActionResult(
                action_id=action_id,
                action_type=action_type,
                success=False,
                status="failed",
                started_at=_iso(wall_start),
                finished_at=_iso(time.time()),
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=getattr(exc, "errno", None) or type(exc).__name__,
                error_message=str(exc),
                metadata={"command": list(command)},
            )


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)
