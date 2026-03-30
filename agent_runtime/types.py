from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional


@dataclass
class MessageEvent:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class ExperimentRequest:
    user_goal: str
    target_database: str = ""
    allowed_anomalies: List[str] = field(default_factory=list)
    execution_window_seconds: int = 120
    risk_level: str = "medium"
    require_confirmation: bool = True
    user_constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DBColumnProfile:
    name: str
    data_type: str
    nullable: bool
    indexed: bool
    cardinality_hint: Optional[int] = None


@dataclass
class DBTableProfile:
    name: str
    row_count: Optional[int] = None
    columns: List[DBColumnProfile] = field(default_factory=list)
    indexes: List[str] = field(default_factory=list)


@dataclass
class DBContextSummary:
    database: str
    tables: List[DBTableProfile] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    distribution: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSpec:
    task_id: str
    agent_type: str
    anomaly_type: str
    title: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    prechecks: List[Dict[str, Any]] = field(default_factory=list)
    execution_steps: List[Dict[str, Any]] = field(default_factory=list)
    validation_steps: List[Dict[str, Any]] = field(default_factory=list)
    rollback_steps: List[Dict[str, Any]] = field(default_factory=list)
    explanation: str = ""


@dataclass
class ExperimentPlan:
    summary: str
    db_context_summary: str
    tasks: List[TaskSpec] = field(default_factory=list)
    expected_signals: List[str] = field(default_factory=list)
    safety_checks: List[str] = field(default_factory=list)
    cleanup_plan: List[str] = field(default_factory=list)
    missing_information: List[str] = field(default_factory=list)


@dataclass
class TaskResult:
    task_id: str
    status: str
    artifacts: Dict[str, Any] = field(default_factory=dict)
    observed_signals: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    cleanup_status: str = "not_run"


@dataclass
class ExperimentResult:
    plan: ExperimentPlan
    task_results: List[TaskResult] = field(default_factory=list)
    status: str = "pending"
    summary: str = ""


@dataclass
class PlannerResponse:
    plan: Optional[ExperimentPlan] = None
    follow_up_questions: List[str] = field(default_factory=list)
    reasoning: str = ""
