"""Strong schemas for post-driven anomaly reproduction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


FACT_SOURCES = {"explicit_post", "post_hypothesis", "agent_inference", "human_input"}
FEASIBILITY_LEVELS = {"exact", "mechanism", "symptom_only", "blocked"}


def _dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _strings(value: Any, name: str) -> list[str]:
    values = _list(value, name)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{name} must contain only strings")
    return values


def _confidence(value: Any, name: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


@dataclass
class EvidenceFact:
    """One claim with provenance instead of an untraceable LLM assertion."""

    key: str
    value: Any
    source: Literal["explicit_post", "post_hypothesis", "agent_inference", "human_input"]
    evidence: str
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceFact":
        data = _dict(data, "fact")
        source = str(data.get("source") or "")
        if source not in FACT_SOURCES:
            raise ValueError(f"fact.source must be one of {sorted(FACT_SOURCES)}")
        key = str(data.get("key") or "").strip()
        evidence = str(data.get("evidence") or "").strip()
        if not key or not evidence:
            raise ValueError("fact.key and fact.evidence are required")
        return cls(key, data.get("value"), source, evidence, _confidence(data.get("confidence"), "fact.confidence"))

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class IncidentSpec:
    dbms: str
    dbms_version: str | None
    summary: str
    symptoms: list[str]
    mechanism: str
    facts: list[EvidenceFact]
    assumptions: list[str]
    unknowns: list[str]
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentSpec":
        data = _dict(data, "incident_spec")
        dbms = str(data.get("dbms") or "unknown").strip().lower()
        summary = str(data.get("summary") or "").strip()
        mechanism = str(data.get("mechanism") or "").strip()
        if not summary or not mechanism:
            raise ValueError("incident_spec.summary and incident_spec.mechanism are required")
        facts = [EvidenceFact.from_dict(item) for item in _list(data.get("facts", []), "incident_spec.facts")]
        if not facts:
            raise ValueError("incident_spec.facts must contain at least one evidence-backed fact")
        return cls(
            dbms=dbms,
            dbms_version=str(data["dbms_version"]) if data.get("dbms_version") is not None else None,
            summary=summary,
            symptoms=_strings(data.get("symptoms", []), "incident_spec.symptoms"),
            mechanism=mechanism,
            facts=facts,
            assumptions=_strings(data.get("assumptions", []), "incident_spec.assumptions"),
            unknowns=_strings(data.get("unknowns", []), "incident_spec.unknowns"),
            confidence=_confidence(data.get("confidence"), "incident_spec.confidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "facts": [item.to_dict() for item in self.facts],
        }


@dataclass
class FeasibilityAssessment:
    level: Literal["exact", "mechanism", "symptom_only", "blocked"]
    rationale: str
    missing_capabilities: list[str]
    unmatched_conditions: list[str]
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeasibilityAssessment":
        data = _dict(data, "feasibility")
        level = str(data.get("level") or "")
        if level not in FEASIBILITY_LEVELS:
            raise ValueError(f"feasibility.level must be one of {sorted(FEASIBILITY_LEVELS)}")
        rationale = str(data.get("rationale") or "").strip()
        if not rationale:
            raise ValueError("feasibility.rationale is required")
        return cls(
            level=level,
            rationale=rationale,
            missing_capabilities=_strings(data.get("missing_capabilities", []), "feasibility.missing_capabilities"),
            unmatched_conditions=_strings(data.get("unmatched_conditions", []), "feasibility.unmatched_conditions"),
            confidence=_confidence(data.get("confidence"), "feasibility.confidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ScaleStrategy:
    initial_rows: int
    max_rows: int
    growth_factor: float
    max_rounds: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScaleStrategy":
        data = _dict(data, "data_spec.scale_strategy")
        result = cls(
            initial_rows=int(data.get("initial_rows", 0)),
            max_rows=int(data.get("max_rows", 0)),
            growth_factor=float(data.get("growth_factor", 0)),
            max_rounds=int(data.get("max_rounds", 3)),
        )
        if result.initial_rows <= 0 or result.max_rows < result.initial_rows:
            raise ValueError("scale_strategy requires 0 < initial_rows <= max_rows")
        if result.growth_factor <= 1 or result.max_rounds <= 0:
            raise ValueError("scale_strategy growth_factor must be >1 and max_rounds must be positive")
        return result

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class DataSpec:
    database: str
    schema_sql: list[str]
    generation_sql: list[str]
    tables: list[dict[str, Any]]
    constraints: dict[str, Any]
    analyze_tables: list[str]
    calibration_queries: list[dict[str, Any]]
    scale_strategy: ScaleStrategy

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataSpec":
        data = _dict(data, "data_spec")
        database = str(data.get("database") or "").strip()
        if not database:
            raise ValueError("data_spec.database is required")
        tables = _list(data.get("tables", []), "data_spec.tables")
        calibrations = _list(data.get("calibration_queries", []), "data_spec.calibration_queries")
        if not all(isinstance(item, dict) for item in tables + calibrations):
            raise ValueError("data_spec tables and calibration_queries must contain objects")
        for index, query in enumerate(calibrations):
            if not str(query.get("sql") or "").strip():
                raise ValueError(f"data_spec.calibration_queries[{index}].sql is required")
        return cls(
            database=database,
            schema_sql=_strings(data.get("schema_sql", []), "data_spec.schema_sql"),
            generation_sql=_strings(data.get("generation_sql", []), "data_spec.generation_sql"),
            tables=tables,
            constraints=_dict(data.get("constraints", {}), "data_spec.constraints"),
            analyze_tables=_strings(data.get("analyze_tables", []), "data_spec.analyze_tables"),
            calibration_queries=calibrations,
            scale_strategy=ScaleStrategy.from_dict(data.get("scale_strategy", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "scale_strategy": self.scale_strategy.to_dict()}


@dataclass
class ReproductionBlueprint:
    incident_spec: IncidentSpec
    feasibility: FeasibilityAssessment
    environment_spec: dict[str, Any]
    data_spec: DataSpec
    workload_spec: dict[str, Any]
    evaluation_spec: dict[str, Any]
    experiment_request: dict[str, Any]
    task_specs: list[dict[str, Any]]
    dependencies: list[list[str]]
    risk_assessment: Literal["low", "medium", "high"]
    requires_domain_judgment: bool
    unresolved_critical_questions: list[str]
    rationale: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReproductionBlueprint":
        data = _dict(data, "blueprint")
        risk = str(data.get("risk_assessment") or "")
        if risk not in {"low", "medium", "high"}:
            raise ValueError("risk_assessment must be low, medium, or high")
        tasks = _list(data.get("task_specs", []), "task_specs")
        if not all(isinstance(task, dict) for task in tasks):
            raise ValueError("task_specs must contain objects")
        for task in tasks:
            if not str(task.get("task_id") or "") or not isinstance(task.get("actions"), list):
                raise ValueError("each TaskSpec requires task_id and actions")
            metadata = _dict(task.get("metadata", {}), "TaskSpec.metadata")
            if not str(metadata.get("root_cause") or ""):
                raise ValueError("each TaskSpec metadata.root_cause is required")
            action_text = str(task.get("actions") or "").upper()
            if "SET GLOBAL" in action_text and not isinstance(task.get("cleanup_actions"), list):
                raise ValueError("TaskSpecs that use SET GLOBAL require cleanup_actions")
            if "SET GLOBAL" in action_text and not task.get("cleanup_actions"):
                raise ValueError("TaskSpecs that use SET GLOBAL require non-empty cleanup_actions")
        dependencies = _list(data.get("dependencies", []), "dependencies")
        if not all(isinstance(edge, list) and len(edge) == 2 and all(isinstance(v, str) for v in edge) for edge in dependencies):
            raise ValueError("dependencies must contain [source, target] string pairs")
        evaluation = _dict(data.get("evaluation_spec", {}), "evaluation_spec")
        if not isinstance(evaluation.get("validation_criteria"), list):
            raise ValueError("evaluation_spec.validation_criteria must be an array")
        experiment_request = _dict(data.get("experiment_request", {}), "experiment_request")
        if not str(experiment_request.get("target_database") or "").strip():
            raise ValueError("experiment_request.target_database is required")
        if int(experiment_request.get("max_duration_sec", 0) or 0) <= 0:
            raise ValueError("experiment_request.max_duration_sec must be positive")
        if experiment_request.get("risk_level") not in {"low", "medium", "high"}:
            raise ValueError("experiment_request.risk_level must be low, medium, or high")
        rationale = str(data.get("rationale") or "").strip()
        if not rationale:
            raise ValueError("rationale is required")
        return cls(
            incident_spec=IncidentSpec.from_dict(data.get("incident_spec", {})),
            feasibility=FeasibilityAssessment.from_dict(data.get("feasibility", {})),
            environment_spec=_dict(data.get("environment_spec", {}), "environment_spec"),
            data_spec=DataSpec.from_dict(data.get("data_spec", {})),
            workload_spec=_dict(data.get("workload_spec", {}), "workload_spec"),
            evaluation_spec=evaluation,
            experiment_request=experiment_request,
            task_specs=tasks,
            dependencies=dependencies,
            risk_assessment=risk,
            requires_domain_judgment=bool(data.get("requires_domain_judgment", False)),
            unresolved_critical_questions=_strings(data.get("unresolved_critical_questions", []), "unresolved_critical_questions"),
            rationale=rationale,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "incident_spec": self.incident_spec.to_dict(),
            "feasibility": self.feasibility.to_dict(),
            "data_spec": self.data_spec.to_dict(),
        }


@dataclass
class ReproductionEvaluation:
    symptom_hit: bool
    mechanism_hit: bool
    plan_similarity: float
    success: bool
    reason: str
    unmatched_conditions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReproductionEvaluation":
        data = _dict(data, "evaluation")
        return cls(
            symptom_hit=bool(data.get("symptom_hit")),
            mechanism_hit=bool(data.get("mechanism_hit")),
            plan_similarity=_confidence(data.get("plan_similarity", 0), "evaluation.plan_similarity"),
            success=bool(data.get("success")),
            reason=str(data.get("reason") or ""),
            unmatched_conditions=_strings(data.get("unmatched_conditions", []), "evaluation.unmatched_conditions"),
            evidence=_dict(data.get("evidence", {}), "evaluation.evidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
