from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ExperimentContext:
    repo_root: Path
    output_dir: Path
    chain_id: str
    chain: dict[str, Any]
    graph: dict[str, Any]
    params: dict[str, Any]


@dataclass
class VerificationResult:
    node_id: str
    hit: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChainRunResult:
    chain_id: str
    complete: bool
    output_dir: Path
    verifier_results: list[VerificationResult]
    raw_summary: dict[str, Any] = field(default_factory=dict)
    tuning_history: list[dict[str, Any]] = field(default_factory=list)


class Injector(Protocol):
    name: str

    def setup(self, context: ExperimentContext) -> None:
        ...

    def inject(self, context: ExperimentContext) -> dict[str, Any]:
        ...

    def cleanup(self, context: ExperimentContext) -> None:
        ...


class Observer(Protocol):
    name: str

    def sample(self, context: ExperimentContext) -> dict[str, Any]:
        ...


class Verifier(Protocol):
    name: str

    def verify(self, baseline: dict[str, Any], current: dict[str, Any], history: list[dict[str, Any]]) -> VerificationResult:
        ...
