"""Deterministic protocol and database-object validation for ExecutableAction."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..models.action import ActionBase, BenchBaseAction, ChaosBladeAction, SQLAction, TransactionAction
from ..tools.database.base import ToolResult


class ValidationDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    REGENERATE = "REGENERATE"


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field: str | None = None


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ValidationDecision
    action_id: str
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_fields: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision == ValidationDecision.PASS


class ActionValidator:
    """Validate action shape, target, limits and referenced DB objects.

    This class never changes an Action. A caller must either execute the exact
    validated object or ask the Agent to generate a new one.
    """

    def __init__(
        self,
        *,
        target_database: str,
        schema_provider: Any | None = None,
        allowed_target_nodes: set[str] | None = None,
        max_duration_sec: int = 300,
        max_concurrency: int = 64,
        allowed_path_roots: Iterable[str | Path] = (),
    ) -> None:
        self.target_database = target_database
        self.schema_provider = schema_provider
        self.allowed_target_nodes = allowed_target_nodes or {
            "traffic_surge",
            "connections_up",
            "row_lock",
            "table_blocking",
            "slow_query",
            "qps_drop",
            "cpu_saturation",
            "memory_pressure",
            "disk_bottleneck",
            "network_latency",
            "resource_pressure",
            "missing_index",
            "backup",
            "connection_storm",
            "deadlock_storm",
            "disk_full_or_pressure",
            "excessive_index",
            "hot_update",
            "improper_sql",
            "large_temp_table",
            "metadata_lock",
            "metadata_lock_wait",
            "missing_index",
            "network_latency",
            "redo_log_pressure",
            "resource_io",
            "resource_memory",
            "resource_cpu",
        }
        self.max_duration_sec = max_duration_sec
        self.max_concurrency = max_concurrency
        self.allowed_path_roots = tuple(Path(root).expanduser().resolve() for root in allowed_path_roots)

    def validate(self, action: ActionBase) -> ValidationResult:
        issues: list[ValidationIssue] = []
        checked: list[str] = ["action_id", "target_node"]
        if action.target_node not in self.allowed_target_nodes:
            issues.append(ValidationIssue(code="UNKNOWN_TARGET_NODE", field="target_node", message="target_node is not registered"))

        if isinstance(action, (SQLAction, TransactionAction, BenchBaseAction)):
            checked.append("database")
            if action.database != self.target_database:
                issues.append(ValidationIssue(code="DATABASE_MISMATCH", field="database", message="action database is not the configured target database"))
            if action.duration_sec > self.max_duration_sec:
                issues.append(ValidationIssue(code="DURATION_OVER_BUDGET", field="duration_sec", message="action duration exceeds validation budget"))

        if isinstance(action, SQLAction):
            checked.append("sql")
            if not action.sql.strip():
                issues.append(ValidationIssue(code="EMPTY_SQL", field="sql", message="SQL must not be empty"))
            if action.concurrency > self.max_concurrency:
                issues.append(ValidationIssue(code="CONCURRENCY_OVER_BUDGET", field="concurrency", message="SQL concurrency exceeds validation budget"))
            issues.extend(self._check_sql_objects(action.database, action.sql))

        elif isinstance(action, TransactionAction):
            checked.append("actors")
            if not action.actors:
                issues.append(ValidationIssue(code="EMPTY_ACTORS", field="actors", message="transaction must contain at least one actor"))
            if len(action.actors) > self.max_concurrency:
                issues.append(ValidationIssue(code="CONCURRENCY_OVER_BUDGET", field="actors", message="actor count exceeds validation budget"))
            actor_ids = [actor.actor_id for actor in action.actors]
            if len(actor_ids) != len(set(actor_ids)):
                issues.append(ValidationIssue(code="DUPLICATE_ACTOR_ID", field="actors", message="actor_id values must be unique"))
            for actor in action.actors:
                if not actor.steps:
                    issues.append(ValidationIssue(code="EMPTY_ACTOR_STEPS", field=f"actors.{actor.actor_id}.steps", message="transaction actor has no steps"))
                for index, step in enumerate(actor.steps):
                    issues.extend(self._check_sql_objects(action.database, step.sql, field=f"actors.{actor.actor_id}.steps[{index}].sql"))

        elif isinstance(action, BenchBaseAction):
            checked.extend(["benchmark", "jar_path", "config_path", "results_dir"])
            for field in ("jar_path", "config_path"):
                value = Path(getattr(action, field)).expanduser()
                if not value.is_file():
                    issues.append(ValidationIssue(code="FILE_NOT_FOUND", field=field, message=f"{field} does not exist: {value}"))
                elif not self._path_allowed(value):
                    issues.append(ValidationIssue(code="PATH_NOT_ALLOWED", field=field, message=f"{field} is outside allowed roots"))
            results = Path(action.results_dir).expanduser()
            if not self._path_allowed(results):
                issues.append(ValidationIssue(code="PATH_NOT_ALLOWED", field="results_dir", message="results_dir is outside allowed roots"))

        elif isinstance(action, ChaosBladeAction):
            checked.extend(["resource", "command", "blade_path"])

        return ValidationResult(
            decision=ValidationDecision.PASS if not issues else ValidationDecision.REGENERATE,
            action_id=action.action_id,
            issues=issues,
            checked_fields=checked,
        )

    def _check_sql_objects(self, database: str, sql: str, *, field: str = "sql") -> list[ValidationIssue]:
        references = _extract_references(sql)
        if not references:
            return []
        schema = self._schema(database)
        if schema is None:
            return [ValidationIssue(code="SCHEMA_UNAVAILABLE", field=field, message="schema metadata is required to validate referenced objects")]
        issues: list[ValidationIssue] = []
        for table, columns in references:
            if table not in schema:
                issues.append(ValidationIssue(code="UNKNOWN_TABLE", field=field, message=f"table does not exist: {table}"))
                continue
            for column in columns:
                if column != "*" and column not in schema[table]:
                    issues.append(ValidationIssue(code="UNKNOWN_COLUMN", field=field, message=f"column does not exist: {table}.{column}"))
        return issues

    def _schema(self, database: str) -> dict[str, set[str]] | None:
        if self.schema_provider is None:
            return None
        result = self.schema_provider.probe_schema(database) if hasattr(self.schema_provider, "probe_schema") else self.schema_provider(database)
        if isinstance(result, ToolResult):
            if not result.success or not isinstance(result.data, dict):
                return None
            tables = result.data.get("tables", [])
        else:
            tables = result.get("tables", []) if isinstance(result, dict) else []
        return {table["table_name"]: {column["name"] for column in table.get("columns", [])} for table in tables}

    def _path_allowed(self, path: Path) -> bool:
        if not self.allowed_path_roots:
            return True
        candidate = path.resolve()
        return any(candidate == root or root in candidate.parents for root in self.allowed_path_roots)


def _extract_references(sql: str) -> list[tuple[str, set[str]]]:
    normalized = sql.replace("`", "")
    table_match = re.search(r"\b(?:FROM|UPDATE|INTO|JOIN)\s+([A-Za-z_][\w$]*)(?:\s+AS\s+\w+|\s+\w+)?", normalized, re.IGNORECASE)
    if not table_match:
        return []
    table = table_match.group(1)
    columns: set[str] = set()
    select_match = re.search(r"\bSELECT\s+(.+?)\s+FROM\b", normalized, re.IGNORECASE | re.DOTALL)
    if select_match:
        for token in select_match.group(1).split(","):
            token = token.strip().split()[0]
            if re.match(r"^[A-Za-z_]\w*$", token):
                columns.add(token)
    set_match = re.search(r"\bSET\s+(.+?)(?:\s+WHERE\b|\s+ORDER\b|\s+LIMIT\b|$)", normalized, re.IGNORECASE | re.DOTALL)
    if set_match:
        for assignment in set_match.group(1).split(","):
            name = assignment.split("=", 1)[0].strip()
            if re.match(r"^[A-Za-z_]\w*$", name):
                columns.add(name)
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:=|<>|!=|>=|<=|>|<|LIKE|IS)\b", normalized, re.IGNORECASE):
        columns.add(match.group(1))
    return [(table, columns)]
