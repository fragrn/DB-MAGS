"""Persistent Human-in-the-loop gates and resumable run state."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


WAITING_EXIT_CODE = 2
EDITABLE_ROOTS = {
    "incident_spec.assumptions",
    "incident_spec.unknowns",
    "data_spec.constraints",
    "data_spec.scale_strategy",
    "data_spec.tables",
    "workload_spec",
    "task_specs",
    "dependencies",
    "evaluation_spec",
    "experiment_request",
    "risk_assessment",
}


@dataclass
class RunState:
    run_id: str
    status: Literal["running", "waiting_human", "completed", "failed", "rejected"]
    phase: str
    interaction: Literal["interactive", "checkpoint"]
    completed_phases: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    failed_rounds: int = 0
    pending_gate: dict[str, Any] | None = None
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        return cls(
            run_id=str(data["run_id"]),
            status=data["status"],
            phase=str(data["phase"]),
            interaction=data.get("interaction", "checkpoint"),
            completed_phases=list(data.get("completed_phases", [])),
            artifacts=dict(data.get("artifacts", {})),
            failed_rounds=int(data.get("failed_rounds", 0)),
            pending_gate=data.get("pending_gate"),
            last_error=str(data.get("last_error", "")),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = time.time()
        return self.__dict__.copy()


@dataclass
class HumanDecision:
    decision: Literal["approve", "reject", "revise", "feedback", "retry"]
    patch: dict[str, Any] | None = None
    feedback: str = ""
    actor: str = "human"

    def validate(self) -> None:
        if self.decision not in {"approve", "reject", "revise", "feedback", "retry"}:
            raise ValueError("decision must be approve, reject, revise, feedback, or retry")
        if self.decision == "revise" and not isinstance(self.patch, dict):
            raise ValueError("revise requires a JSON merge patch object")
        if self.decision == "feedback" and not self.feedback.strip():
            raise ValueError("feedback decision requires non-empty feedback")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {**self.__dict__, "timestamp": time.time()}


class HumanGateRequired(RuntimeError):
    def __init__(self, run_dir: Path, request: dict[str, Any]):
        super().__init__(request.get("summary") or "Human approval required")
        self.run_dir = Path(run_dir)
        self.request = request


def gate_reasons(blueprint: dict[str, Any], failed_rounds: int = 0) -> list[str]:
    """Return policy reasons that require a human decision."""
    incident = blueprint.get("incident_spec") or {}
    feasibility = blueprint.get("feasibility") or {}
    reasons: list[str] = []
    if float(incident.get("confidence", 0) or 0) < 0.70:
        reasons.append("incident_confidence_below_0.70")
    if float(feasibility.get("confidence", 0) or 0) < 0.70:
        reasons.append("feasibility_confidence_below_0.70")
    if feasibility.get("level") in {"symptom_only", "blocked"}:
        reasons.append(f"feasibility_{feasibility.get('level')}")
    if blueprint.get("risk_assessment") == "high":
        reasons.append("high_risk")
    if blueprint.get("requires_domain_judgment"):
        reasons.append("domain_judgment_required")
    if blueprint.get("unresolved_critical_questions"):
        reasons.append("unresolved_critical_questions")
    if _contains_high_risk_operation(blueprint):
        reasons.append("privileged_or_global_operation")
    if failed_rounds >= 2:
        reasons.append("two_failed_reproduction_rounds")
    return list(dict.fromkeys(reasons))


def write_gate(run_dir: Path, state: RunState, *, phase: str, reasons: list[str], summary: str) -> dict[str, Any]:
    request = {
        "status": "waiting_human",
        "phase": phase,
        "reasons": reasons,
        "summary": summary,
        "allowed_decisions": ["approve", "reject", "revise", "feedback"],
        "editable_roots": sorted(EDITABLE_ROOTS),
        "timestamp": time.time(),
    }
    state.status = "waiting_human"
    state.phase = phase
    state.pending_gate = request
    write_json(run_dir / "hitl_request.json", request)
    save_state(run_dir, state)
    return request


def apply_controlled_patch(document: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply JSON Merge Patch only below explicitly editable roots."""
    flattened = _flatten_patch(patch)
    forbidden = [path for path in flattened if not any(path == root or path.startswith(root + ".") for root in EDITABLE_ROOTS)]
    if forbidden:
        raise ValueError(f"patch contains non-editable fields: {', '.join(sorted(forbidden))}")
    return _merge_patch(document, patch)


def save_state(run_dir: Path, state: RunState) -> None:
    write_json(run_dir / "state.json", state.to_dict())


def load_state(run_dir: Path) -> RunState:
    path = Path(run_dir) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"state file not found: {path}")
    return RunState.from_dict(json.loads(path.read_text()))


def record_decision(run_dir: Path, decision: HumanDecision) -> None:
    payload = decision.to_dict()
    write_json(Path(run_dir) / "hitl_response.json", payload)
    history = Path(run_dir) / "decision_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _flatten_patch(value: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict) and child:
            paths.extend(_flatten_patch(child, path))
        else:
            paths.append(path)
    return paths


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _contains_high_risk_operation(blueprint: dict[str, Any]) -> bool:
    text = json.dumps(
        {
            "data_spec": blueprint.get("data_spec", {}),
            "task_specs": blueprint.get("task_specs", []),
            "workload_spec": blueprint.get("workload_spec", {}),
        },
        ensure_ascii=False,
    ).lower()
    markers = (
        "set global",
        "flush tables",
        "restart",
        "shutdown",
        "reboot",
        "rm\", \"-f",
        "chaosblade",
        "blade create",
        "network",
    )
    return any(marker in text for marker in markers)
