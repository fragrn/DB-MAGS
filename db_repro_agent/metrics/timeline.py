"""Experiment clock, phase markers and serializable metric samples."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExperimentPhase(StrEnum):
    WARMUP = "warmup"
    BASELINE = "baseline"
    INJECTION = "injection"
    OBSERVING = "observing"
    RECOVERY = "recovery"
    STOPPED = "stopped"


class TimelineMarker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: str = "marker"
    timestamp: datetime
    phase: ExperimentPhase
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: str = "sample"
    timestamp: datetime
    phase: ExperimentPhase
    metric_name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)


class ExperimentTimeline:
    def __init__(self, *, started_at: datetime | None = None) -> None:
        self.started_at = _aware(started_at or datetime.now(timezone.utc))
        self._markers: list[TimelineMarker] = []
        self._samples: list[MetricSample] = []
        self._lock = RLock()
        self.mark(ExperimentPhase.WARMUP, name="workload_start")

    @property
    def markers(self) -> list[TimelineMarker]:
        with self._lock:
            return list(self._markers)

    @property
    def samples(self) -> list[MetricSample]:
        with self._lock:
            return list(self._samples)

    @property
    def current_phase(self) -> ExperimentPhase:
        with self._lock:
            return self._markers[-1].phase if self._markers else ExperimentPhase.WARMUP

    def mark(self, phase: ExperimentPhase, *, name: str | None = None, metadata: dict[str, Any] | None = None) -> TimelineMarker:
        marker = TimelineMarker(
            timestamp=datetime.now(timezone.utc),
            phase=phase,
            name=name or phase.value,
            metadata=metadata or {},
        )
        with self._lock:
            self._markers.append(marker)
        return marker

    def add_sample(
        self,
        metric_name: str,
        value: float,
        *,
        phase: ExperimentPhase | None = None,
        timestamp: datetime | None = None,
        labels: dict[str, str] | None = None,
    ) -> MetricSample:
        sample = MetricSample(
            timestamp=_aware(timestamp or datetime.now(timezone.utc)),
            phase=phase or self.current_phase,
            metric_name=metric_name,
            value=float(value),
            labels=labels or {},
        )
        with self._lock:
            self._samples.append(sample)
        return sample

    def phase_at(self, timestamp: datetime) -> ExperimentPhase:
        point = _aware(timestamp)
        with self._lock:
            phase = self._markers[0].phase if self._markers else ExperimentPhase.WARMUP
            for marker in self._markers:
                if marker.timestamp <= point:
                    phase = marker.phase
                else:
                    break
            return phase

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [marker.model_dump(mode="json") for marker in self._markers]
            records.extend(sample.model_dump(mode="json") for sample in self._samples)
        return sorted(records, key=lambda item: item["timestamp"])

    def write_jsonl(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for record in self.records():
                import json

                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return destination


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
