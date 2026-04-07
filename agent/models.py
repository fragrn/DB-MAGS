from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ColumnProfile:
    name: str
    data_type: str
    is_nullable: bool
    column_key: str
    cardinality_estimate: Optional[int] = None
    null_ratio: Optional[float] = None
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None


@dataclass
class TableProfile:
    name: str
    row_count_estimate: int
    columns: List[ColumnProfile] = field(default_factory=list)
    indexes: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class DatabaseProfile:
    schema_name: str
    tables: List[TableProfile] = field(default_factory=list)
    collected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TaskSpec:
    task_id: str
    task_type: str
    agent_name: str
    start_after_seconds: int
    duration_seconds: int
    payload: Dict[str, Any] = field(default_factory=dict)
    cleanup_payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    task_type: str
    agent_name: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    plan: List[TaskSpec]
    results: List[TaskResult]
    output_dir: str
    runtime: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "openai_available": bool(self.runtime.get("openai_available")),
            "openai_model": self.runtime.get("openai_model"),
            "openai_connected": bool(self.runtime.get("openai_connected")),
            "openai_error": self.runtime.get("openai_error"),
            "openai_endpoint": self.runtime.get("openai_endpoint"),
            "planner_summary": self.runtime.get("planner_summary"),
            "plan": [asdict(item) for item in self.plan],
            "results": [asdict(item) for item in self.results],
            "output_dir": self.output_dir,
        }
