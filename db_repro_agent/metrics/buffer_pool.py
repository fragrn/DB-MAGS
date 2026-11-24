"""Buffer pool counter deltas and experiment-window summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .timeline import ExperimentPhase, MetricSample


@dataclass(frozen=True)
class BufferPoolDelta:
    reads_delta: float
    requests_delta: float
    miss_ratio: float | None
    physical_reads_per_sec: float | None
    valid: bool


def derive_buffer_pool_delta(
    previous_reads: float,
    current_reads: float,
    previous_requests: float,
    current_requests: float,
    elapsed_seconds: float,
) -> BufferPoolDelta:
    """Calculate one adjacent counter interval without producing negative deltas."""
    if (
        current_reads < previous_reads
        or current_requests < previous_requests
        or elapsed_seconds <= 0
    ):
        return BufferPoolDelta(0.0, 0.0, None, None, False)

    reads_delta = current_reads - previous_reads
    requests_delta = current_requests - previous_requests
    if requests_delta <= 0:
        return BufferPoolDelta(reads_delta, requests_delta, None, None, False)

    return BufferPoolDelta(
        reads_delta=reads_delta,
        requests_delta=requests_delta,
        miss_ratio=reads_delta / requests_delta,
        physical_reads_per_sec=reads_delta / elapsed_seconds,
        valid=True,
    )


def summarize_buffer_pool_window(
    samples: Iterable[MetricSample],
    phases: set[ExperimentPhase],
) -> dict[str, float | bool]:
    """Summarize adjacent raw counter intervals wholly inside selected phases."""
    reads = [
        sample
        for sample in samples
        if sample.metric_name in {"mysql.innodb_buffer_pool_reads", "innodb_buffer_pool_reads"}
    ]
    requests = [
        sample
        for sample in samples
        if sample.metric_name
        in {"mysql.innodb_buffer_pool_read_requests", "innodb_buffer_pool_read_requests"}
    ]
    reads.sort(key=lambda sample: sample.timestamp)
    requests.sort(key=lambda sample: sample.timestamp)

    valid_intervals: list[BufferPoolDelta] = []
    for index in range(1, min(len(reads), len(requests))):
        current_reads = reads[index]
        current_requests = requests[index]
        previous_reads = reads[index - 1]
        previous_requests = requests[index - 1]
        if current_reads.phase not in phases:
            continue
        if current_requests.phase not in phases:
            continue
        if phases == {ExperimentPhase.BASELINE} and (
            previous_reads.phase not in phases or previous_requests.phase not in phases
        ):
            continue
        elapsed = (current_reads.timestamp - previous_reads.timestamp).total_seconds()
        interval = derive_buffer_pool_delta(
            previous_reads=previous_reads.value,
            current_reads=current_reads.value,
            previous_requests=previous_requests.value,
            current_requests=current_requests.value,
            elapsed_seconds=elapsed,
        )
        if interval.valid:
            valid_intervals.append(interval)

    total_reads = sum(interval.reads_delta for interval in valid_intervals)
    total_requests = sum(interval.requests_delta for interval in valid_intervals)
    miss_ratio_valid = total_requests > 0
    physical_values = [
        interval.physical_reads_per_sec
        for interval in valid_intervals
        if interval.physical_reads_per_sec is not None
    ]
    physical_valid = bool(physical_values)
    return {
        "miss_ratio": total_reads / total_requests if miss_ratio_valid else 0.0,
        "miss_ratio_valid": miss_ratio_valid,
        "physical_reads_per_sec": sum(physical_values) / len(physical_values)
        if physical_valid
        else 0.0,
        "physical_reads_valid": physical_valid,
    }
