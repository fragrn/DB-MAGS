"""CPU, memory and IO metrics with optional psutil acceleration."""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Callable


class SystemMetricsProvider:
    def __init__(self, reader: Callable[[], dict[str, float]] | None = None) -> None:
        self.reader = reader
        self._previous_disk = None
        self._previous_at = None

    def collect(self) -> dict[str, float]:
        if self.reader is not None:
            return {key: float(value) for key, value in self.reader().items()}
        try:
            import psutil  # type: ignore

            virtual_memory = psutil.virtual_memory()
            disk = psutil.disk_io_counters()
            cpu_times = psutil.cpu_times()
            metrics = {
                "cpu_percent": float(psutil.cpu_percent(interval=None)),
                "memory_percent": float(virtual_memory.percent),
                "memory_used_bytes": float(virtual_memory.used),
                # macOS does not expose iowait on every host; keep the field
                # explicit so IO evidence can distinguish zero from missing.
                "io_wait_ratio": float(getattr(cpu_times, "iowait", 0.0))
                / max(float(sum(cpu_times)), 1.0),
                "disk_util": 0.0,
            }
            if disk is not None:
                now = time.monotonic()
                if self._previous_disk is not None and self._previous_at is not None:
                    elapsed = max(now - self._previous_at, 1e-9)
                    metrics["io_read_bytes_per_sec"] = max(0.0, disk.read_bytes - self._previous_disk.read_bytes) / elapsed
                    metrics["io_write_bytes_per_sec"] = max(0.0, disk.write_bytes - self._previous_disk.write_bytes) / elapsed
                    busy_ms = getattr(disk, "busy_time", None)
                    previous_busy_ms = getattr(self._previous_disk, "busy_time", None)
                    if busy_ms is not None and previous_busy_ms is not None:
                        metrics["disk_util"] = min(1.0, max(0.0, (busy_ms - previous_busy_ms) / (elapsed * 1000.0)))
                else:
                    metrics["io_read_bytes_per_sec"] = 0.0
                    metrics["io_write_bytes_per_sec"] = 0.0
                self._previous_disk = disk
                self._previous_at = now
            return metrics
        except ImportError:
            return self._fallback_metrics()

    @staticmethod
    def _fallback_metrics() -> dict[str, float]:
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        cpu_count = max(os.cpu_count() or 1, 1)
        disk = shutil.disk_usage("/")
        return {
            "cpu_percent": min(100.0, load / cpu_count * 100.0),
            "memory_percent": 0.0,
            "memory_used_bytes": 0.0,
            "io_read_bytes_per_sec": 0.0,
            "io_write_bytes_per_sec": 0.0,
            "io_wait_ratio": 0.0,
            "disk_util": 0.0,
            "disk_free_bytes": float(disk.free),
        }
