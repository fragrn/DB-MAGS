"""Metric provider aggregation; providers are isolated from the sampler thread."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .timeline import ExperimentPhase, ExperimentTimeline
from ..models.evidence import EvidenceSnapshot, SnapshotBoundary


class MetricProvider(Protocol):
    def collect(self) -> dict[str, float]: ...


class MetricsCollector:
    def __init__(self, providers: dict[str, MetricProvider], logger: logging.Logger | None = None) -> None:
        self.providers = providers
        self.logger = logger or logging.getLogger("db_repro_agent.metrics")

    def collect_once(self, timeline: ExperimentTimeline, *, phase: ExperimentPhase | None = None) -> dict[str, float]:
        collected: dict[str, float] = {}
        for provider_name, provider in self.providers.items():
            try:
                values = provider.collect()
                for metric_name, value in values.items():
                    full_name = metric_name if provider_name == "" else f"{provider_name}.{metric_name}"
                    timeline.add_sample(full_name, value, phase=phase)
                    collected[full_name] = float(value)
            except Exception as exc:
                self.logger.warning("metric provider failed provider=%s error=%s", provider_name, exc)
        return collected

    def snapshot(self, boundary: SnapshotBoundary) -> EvidenceSnapshot:
        metrics: dict[str, float] = {}
        supporting_data: dict[str, Any] = {}
        errors: list[str] = []
        for provider_name, provider in self.providers.items():
            try:
                snapshot_fn = getattr(provider, "snapshot", None)
                values = snapshot_fn() if snapshot_fn is not None else provider.collect()
                for metric_name, value in values.items():
                    qualified = metric_name if not provider_name else f"{provider_name}.{metric_name}"
                    metrics[qualified] = float(value)
                supporting_fn = getattr(provider, "snapshot_supporting", None)
                if supporting_fn is not None:
                    supporting_data[provider_name] = supporting_fn()
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
        return EvidenceSnapshot(
            boundary=boundary,
            metrics=metrics,
            supporting_data=supporting_data,
            success=not errors,
            error="; ".join(errors) if errors else None,
        )
