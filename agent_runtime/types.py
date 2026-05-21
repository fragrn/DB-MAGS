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
    target_anomaly: str = ""
    target_mode: str = "single_root"
    target_chain: List[str] = field(default_factory=list)
    target_subgraph: Dict[str, Any] = field(default_factory=dict)
    workload_config: Dict[str, Any] = field(default_factory=dict)
    max_retry_rounds: int = 5
    safety_constraints: Dict[str, Any] = field(default_factory=dict)
    target_database: str = ""
    allowed_anomalies: List[str] = field(default_factory=list)
    allowed_subtypes: List[str] = field(default_factory=list)
    anomaly_categories: List[str] = field(default_factory=list)
    execution_window_seconds: int = 120
    risk_level: str = "medium"
    require_confirmation: bool = True
    execution_mode: str = "sequential"
    database_topology: str = "base_and_copy"
    user_constraints: Dict[str, Any] = field(default_factory=dict)
    mode: str = "single"
    test_enabled: bool = False
    fresh_database_per_run: bool = False
    keep_database: bool = False


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
    task_role: str = "one_shot_sql"
    inputs: Dict[str, Any] = field(default_factory=dict)
    prechecks: List[Dict[str, Any]] = field(default_factory=list)
    execution_steps: List[Dict[str, Any]] = field(default_factory=list)
    validation_steps: List[Dict[str, Any]] = field(default_factory=list)
    rollback_steps: List[Dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    dependencies: List[str] = field(default_factory=list)
    start_after_sec: float = 0.0
    start_condition: Dict[str, Any] = field(default_factory=dict)
    expected_metrics: Dict[str, Any] = field(default_factory=dict)
    local_success_criteria: Dict[str, Any] = field(default_factory=dict)
    risk_assessment: Dict[str, Any] = field(default_factory=dict)
    cleanup_actions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ReActStep:
    thought: str
    action: str
    observation: Dict[str, Any] = field(default_factory=dict)
    decision: str = ""


@dataclass
class EnvironmentSnapshot:
    database: str
    dbms: str = "mysql"
    version: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)
    index_info: Dict[str, Any] = field(default_factory=dict)
    table_stats: Dict[str, Any] = field(default_factory=dict)
    workload_status: Dict[str, Any] = field(default_factory=dict)
    db_metrics: Dict[str, Any] = field(default_factory=dict)
    os_metrics: Dict[str, Any] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class GlobalPlan:
    mode: str = "single_root"
    root_causes_to_inject: List[str] = field(default_factory=list)
    effects_to_observe: List[str] = field(default_factory=list)
    task_agents: List[str] = field(default_factory=list)
    task_dependencies: List[List[str]] = field(default_factory=list)
    evaluation_targets: List[str] = field(default_factory=list)
    safety_constraints: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class TaskAgentInput:
    subgoal: str
    global_context: Dict[str, Any] = field(default_factory=dict)
    environment_snapshot: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    expected_effect: List[str] = field(default_factory=list)
    memory: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskAgentOutput:
    agent_name: str
    subgoal: str
    local_hypothesis: str
    task_spec: TaskSpec
    expected_metrics: Dict[str, Any] = field(default_factory=dict)
    local_success_criteria: Dict[str, Any] = field(default_factory=dict)
    risk_assessment: Dict[str, Any] = field(default_factory=dict)
    safety_constraints: Dict[str, Any] = field(default_factory=dict)
    cleanup_actions: List[Dict[str, Any]] = field(default_factory=list)
    fallback_plan: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    react_trace: List[ReActStep] = field(default_factory=list)


@dataclass
class TaskDAGNode:
    task_id: str
    task_spec: TaskSpec
    start_after_sec: float = 0.0
    start_condition: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskDAGEdge:
    from_task: str
    to_task: str
    condition: str = ""


@dataclass
class TaskDAG:
    tasks: Dict[str, TaskDAGNode] = field(default_factory=dict)
    edges: List[TaskDAGEdge] = field(default_factory=list)
    schedule: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class SafetyCheckResult:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checked_constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionTrace:
    task_status: Dict[str, str] = field(default_factory=dict)
    start_time: Dict[str, str] = field(default_factory=dict)
    end_time: Dict[str, str] = field(default_factory=dict)
    stdout: Dict[str, Any] = field(default_factory=dict)
    stderr: Dict[str, Any] = field(default_factory=dict)
    errors: Dict[str, Any] = field(default_factory=dict)
    cleanup_status: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    after_metrics: Dict[str, Any] = field(default_factory=dict)
    target_anomaly_scores: Dict[str, float] = field(default_factory=dict)
    reward: Dict[str, Any] = field(default_factory=dict)
    chain_events: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = False
    reason: str = ""


@dataclass
class ReflectionResult:
    failure_reason: List[str] = field(default_factory=list)
    suggested_changes: List[str] = field(default_factory=list)
    risk_warning: List[str] = field(default_factory=list)
    memory_update: List[str] = field(default_factory=list)
    raw_text: str = ""


@dataclass
class MemoryItem:
    dbms: str
    workload: str
    anomaly_type: str
    context: str
    lesson: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class PlannedAnomaly:
    anomaly_subtype: str
    category: str
    source_agent: str
    database: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    expected_signals: List[str] = field(default_factory=list)
    cleanup_strategy: List[str] = field(default_factory=list)


@dataclass
class PlannerDecision:
    selected_anomalies: List[str] = field(default_factory=list)
    task_assignments: Dict[str, str] = field(default_factory=dict)
    database_mapping: Dict[str, str] = field(default_factory=dict)
    task_parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    expected_signals: List[str] = field(default_factory=list)
    cleanup_strategy: List[str] = field(default_factory=list)
    planned_tasks: List[PlannedAnomaly] = field(default_factory=list)
    llm_summary: str = ""
    selection_mode: str = "single"
    execution_database: str = ""
    activation_order: List[str] = field(default_factory=list)
    cleanup_order: List[str] = field(default_factory=list)
    composite_experiment_name: str = ""
    selection_rationale: str = ""
    llm_used: bool = False
    llm_error: str = ""
    llm_error_type: str = ""
    llm_transport: str = ""
    global_plan: Optional[GlobalPlan] = None


@dataclass
class ExperimentPlan:
    summary: str
    db_context_summary: str
    tasks: List[TaskSpec] = field(default_factory=list)
    planner_decision: Optional[PlannerDecision] = None
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
    planner_decision: Optional[PlannerDecision] = None
    follow_up_questions: List[str] = field(default_factory=list)
    reasoning: str = ""
