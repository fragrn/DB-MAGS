"""
Dataclass types for the agent system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NodeCategory(str, Enum):
    INJECTABLE = "injectable"          # can be actively triggered
    INTERMEDIATE = "intermediate"     # observable intermediate state
    SYMPTOM = "symptom"               # terminal anomaly symptom
    EVIDENCE = "evidence"             # metric-level evidence node


class EdgeRelation(str, Enum):
    CAUSES = "causes"
    AMPLIFIES = "amplifies"
    ENABLES = "enables"
    CORRELATES_WITH = "correlates_with"


# ---------------------------------------------------------------------------
# Evidence & Graph
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRule:
    """One rule for determining whether an anomaly node was hit."""
    metric: str
    operator: Literal[">", ">=", "<", "<=", "contains", "exists", "ratio_up", "ratio_down"]
    threshold: Any
    baseline_adjusted: bool = True
    required: bool = False
    weight: float = 1.0
    window_sec: float = 0.0  # time window after injection to look for this signal


@dataclass
class AnomalyNode:
    """A single node in the anomaly propagation graph."""
    node_id: str
    label: str
    description: str
    category: NodeCategory
    injectable: bool = False
    observable: bool = True
    evidence_rules: list[EvidenceRule] = field(default_factory=list)
    # Which tool to use when generating a TaskSpec for this node
    default_tool: str = ""
    # Human-readable notes for the planner
    planner_notes: str = ""


@dataclass
class AnomalyEdge:
    """A directed edge in the anomaly propagation graph."""
    src: str
    dst: str
    relation: EdgeRelation = EdgeRelation.CAUSES
    strength: float = 1.0
    required: bool = True


@dataclass
class AnomalyGraph:
    """The full hardcoded anomaly propagation graph."""
    nodes: dict[str, AnomalyNode] = field(default_factory=dict)
    edges: list[AnomalyEdge] = field(default_factory=list)

    def node(self, node_id: str) -> AnomalyNode | None:
        return self.nodes.get(node_id)

    def predecessors(self, node_id: str) -> list[str]:
        return [e.src for e in self.edges if e.dst == node_id]

    def successors(self, node_id: str) -> list[str]:
        return [e.dst for e in self.edges if e.src == node_id]

    def reachable_paths(self, source: str, target: str) -> list[list[str]]:
        """Return all simple paths from source to target (DFS, bounded depth)."""
        paths: list[list[str]] = []

        def dfs(current: str, path: list[str], visited: set[str]):
            if current == target:
                paths.append(path[:])
                return
            if len(path) > 10:
                return
            for nxt in self.successors(current):
                if nxt not in visited:
                    visited.add(nxt)
                    path.append(nxt)
                    dfs(nxt, path, visited)
                    path.pop()
                    visited.remove(nxt)

        visited = {source}
        dfs(source, [source], visited)
        return paths

    def injectable_nodes(self) -> list[AnomalyNode]:
        return [n for n in self.nodes.values() if n.injectable]


# ---------------------------------------------------------------------------
# Request & Snapshot
# ---------------------------------------------------------------------------


@dataclass
class ExperimentRequest:
    """User input for an anomaly reproduction experiment."""
    target_anomaly: str = ""                    # optional display label
    target_database: str = "testdb"
    dba_description: str = ""                   # natural-language description
    target_path: list[str] = field(default_factory=list)    # user-specified full propagation path
    injected_nodes: list[str] = field(default_factory=list) # user-specified injectable nodes to reproduce
    target_chain: list[str] = field(default_factory=list)   # legacy alias for target_path
    max_duration_sec: int = 300
    max_retry_rounds: int = 5
    risk_level: Literal["low", "medium", "high"] = "medium"
    allowed_anomalies: list[str] = field(default_factory=list)
    safety_overrides: dict[str, Any] = field(default_factory=dict)
    workload: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> ExperimentRequest:
        import copy
        d = copy.deepcopy(data)
        target_path = d.pop("target_path", d.pop("target_chain", []))
        injected_nodes = d.pop("injected_nodes", d.pop("inject_nodes", []))
        target_anomaly = d.pop("target_anomaly", d.pop("target", ""))
        if not target_anomaly and target_path:
            target_anomaly = str(target_path[-1])
        return cls(
            target_anomaly=target_anomaly,
            target_database=d.pop("target_database", d.pop("database", "testdb")),
            dba_description=d.pop("dba_description", ""),
            target_path=target_path,
            injected_nodes=injected_nodes,
            target_chain=list(target_path),
            max_duration_sec=int(d.pop("max_duration_sec", 300)),
            max_retry_rounds=int(d.pop("max_retry_rounds", 5)),
            risk_level=d.pop("risk_level", "medium"),
            allowed_anomalies=d.pop("allowed_anomalies", []),
            safety_overrides=d.pop("safety_overrides", {}),
            workload=d.pop("workload", {}),
            source_path=d.pop("source_path", ""),
        )

    def to_dict(self) -> dict:
        return {
            "target_anomaly": self.target_anomaly,
            "target_database": self.target_database,
            "dba_description": self.dba_description,
            "target_path": self.target_path,
            "injected_nodes": self.injected_nodes,
            "target_chain": self.target_chain,
            "max_duration_sec": self.max_duration_sec,
            "max_retry_rounds": self.max_retry_rounds,
            "risk_level": self.risk_level,
            "allowed_anomalies": self.allowed_anomalies,
            "safety_overrides": self.safety_overrides,
            "workload": self.workload,
            "source_path": self.source_path,
        }


@dataclass
class SchemaInfo:
    database: str
    tables: dict[str, dict]  # table_name -> {columns: [...], indexes: [...], row_count: int, constraints: [...]}


@dataclass
class EnvironmentSnapshot:
    """Aggregated environment state from probes."""
    database: str
    schema: SchemaInfo | None = None
    db_metrics: dict = field(default_factory=dict)
    workload_status: dict = field(default_factory=dict)
    os_metrics: dict = field(default_factory=dict)
    db_version: str = ""
    max_connections: int = 100
    react_trace: list[ReActStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---------------------------------------------------------------------------
# ReAct
# ---------------------------------------------------------------------------


@dataclass
class ReActStep:
    """One step in a ReAct reasoning trace."""
    round: int
    thought: str
    action: str
    observe: str = ""
    decision: str = ""
    timestamp: str = field(default_factory=lambda: _now())

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "thought": self.thought,
            "action": self.action,
            "observe": self.observe,
            "decision": self.decision,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Task DAG
# ---------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """One executable task specification."""
    task_id: str
    task_type: str                          # e.g. "slow_sql", "traffic_surge", "lock_conflict", "backup", "chaos"
    actions: list[dict] = field(default_factory=list)
    expected_metrics: dict = field(default_factory=dict)
    success_criteria: dict = field(default_factory=dict)
    start_after_sec: float = 0.0
    dependencies: list[str] = field(default_factory=list)
    cleanup_actions: list[dict] = field(default_factory=list)
    risk_assessment: str = "medium"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass
class TaskDAGEdge:
    source: str
    target: str
    condition: str = ""


@dataclass
class ExecutableTaskDAG:
    tasks: dict[str, TaskSpec] = field(default_factory=dict)
    edges: list[TaskDAGEdge] = field(default_factory=list)
    schedule: dict[str, float] = field(default_factory=dict)  # task_id -> scheduled_start_offset


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class NodeResult:
    node_id: str
    hit: bool
    confidence: float
    evidence: dict = field(default_factory=dict)
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "hit": self.hit,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "details": self.details,
        }


@dataclass
class PathResult:
    target_path: list[str]
    node_hit_ratio: float
    ordered_hits: list[str]
    broken_edge: tuple[str, str] | None = None
    path_hit: bool = False
    failure_stage: str = ""
    node_results: dict[str, NodeResult] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    performance_score: float = 0.0
    target_anomaly_score: float = 0.0
    causal_order_score: float = 0.0
    stability_score: float = 1.0
    safety_penalty: float = 0.0
    final_score: float = 0.0
    success: bool = False
    reason: str = ""
    detected_events: list[str] = field(default_factory=list)
    failed_nodes: list[str] = field(default_factory=list)
    causal_checks: list[dict] = field(default_factory=list)
    safety_violations: list[str] = field(default_factory=list)
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    path_result: PathResult | None = None
    baseline_metrics: dict = field(default_factory=dict)
    after_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------


@dataclass
class ReflectionResult:
    failure_reason: str = ""
    suggested_changes: list[str] = field(default_factory=list)
    task_parameter_updates: dict = field(default_factory=dict)
    risk_warning: str = ""
    memory_update: list[dict] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    task_id: str
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    start_time: str = ""
    end_time: str = ""
    stdout: str = ""
    stderr: str = ""
    errors: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class ExecutionTrace:
    tasks: dict[str, TaskResult] = field(default_factory=dict)
    cleanup_status: str = "not_run"
    cleanup_errors: list[str] = field(default_factory=list)
    safety_events: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


@dataclass
class SafetyResult:
    approved: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryItem:
    anomaly: str
    path: list[str]
    task_params: dict
    outcome: str
    success: bool
    round: int
    node_hit_ratio: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---------------------------------------------------------------------------
# Run Result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run_id: str
    request: ExperimentRequest
    snapshot: EnvironmentSnapshot
    dag: ExecutableTaskDAG
    evaluation: EvaluationResult
    reflection: ReflectionResult | None = None
    execution_trace: ExecutionTrace | None = None
    workload_trace: dict[str, Any] | None = None
    output_dir: str = ""
    rounds: int = 1

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_jsonable(obj: Any) -> dict:
    """Recursively convert dataclasses + dicts + lists to JSON-serializable objects."""
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if dataclass_isinstance(obj):
        return to_jsonable(_asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    return obj


def _asdict(obj: Any) -> dict:
    if hasattr(obj, "__dataclass_fields__"):
        result = {}
        for name, field_ in obj.__dataclass_fields__.items():
            val = getattr(obj, name)
            if val is None:
                result[name] = None
            elif isinstance(val, (list, tuple)):
                result[name] = [to_jsonable(x) for x in val]
            elif dataclass_isinstance(val):
                result[name] = _asdict(val)
            elif isinstance(val, dict):
                result[name] = {k: to_jsonable(v) for k, v in val.items()}
            elif isinstance(val, Enum):
                result[name] = val.value
            else:
                result[name] = val
        return result
    return obj


def dataclass_isinstance(obj: Any) -> bool:
    return hasattr(obj, "__dataclass_fields__")
