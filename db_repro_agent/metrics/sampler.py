"""Non-blocking background sampler driven by the ExperimentTimeline clock."""

from __future__ import annotations

import threading
import time
from typing import Any

from .collector import MetricsCollector
from .buffer_pool import derive_buffer_pool_delta
from .timeline import ExperimentPhase, ExperimentTimeline
from ..models.evidence import EvidenceSnapshot, SnapshotBoundary


class MetricsSampler:
    def __init__(self, collector: MetricsCollector, *, interval_seconds: float = 1.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.collector = collector
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.timeline: ExperimentTimeline | None = None
        self._previous_buffer_pool: tuple[float, float, float] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeline: ExperimentTimeline, *, phase: ExperimentPhase = ExperimentPhase.WARMUP) -> None:
        if self.running:
            raise RuntimeError("metrics sampler is already running")
        self.timeline = timeline
        self._previous_buffer_pool = None
        timeline.mark(phase, name=f"{phase.value}_start")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metrics-sampler", daemon=True)
        self._thread.start()

    def set_phase(self, phase: ExperimentPhase, *, name: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        if self.timeline is None:
            raise RuntimeError("metrics sampler has not started")
        self.timeline.mark(phase, name=name, metadata=metadata)

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        if self.timeline is not None:
            self.timeline.mark(ExperimentPhase.STOPPED, name="workload_stop")

    def capture_snapshot(self, boundary: SnapshotBoundary) -> EvidenceSnapshot:
        return self.collector.snapshot(boundary)

    def _run(self) -> None:
        assert self.timeline is not None
        while not self._stop.is_set():
            collected = self.collector.collect_once(self.timeline)
            self._record_buffer_pool_delta(collected)
            self._stop.wait(self.interval_seconds)

    def _record_buffer_pool_delta(self, collected: dict[str, float]) -> None:
        reads = _metric_value(collected, "innodb_buffer_pool_reads")
        requests = _metric_value(collected, "innodb_buffer_pool_read_requests")
        if reads is None or requests is None:
            return
        current_time = time.monotonic()
        if self._previous_buffer_pool is None:
            self._previous_buffer_pool = (reads, requests, current_time)
            return
        previous_reads, previous_requests, previous_time = self._previous_buffer_pool
        delta = derive_buffer_pool_delta(
            previous_reads,
            reads,
            previous_requests,
            requests,
            current_time - previous_time,
        )
        phase = self.timeline.current_phase if self.timeline is not None else None
        if phase is not None:
            self.timeline.add_sample(
                "mysql.buffer_pool_miss_ratio_valid",
                1.0 if delta.valid else 0.0,
                phase=phase,
            )
            self.timeline.add_sample(
                "mysql.physical_reads_per_sec",
                delta.physical_reads_per_sec or 0.0,
                phase=phase,
            )
            if delta.miss_ratio is not None:
                self.timeline.add_sample(
                    "mysql.buffer_pool_miss_ratio",
                    delta.miss_ratio,
                    phase=phase,
                )
        self._previous_buffer_pool = (reads, requests, current_time)


def _metric_value(collected: dict[str, float], name: str) -> float | None:
    for key in (f"mysql.{name}", name):
        if key in collected:
            return float(collected[key])
    return None
