"""Non-bypassable safety policy for Agent-generated actions."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Iterable

from ..models.action import ActionBase, BenchBaseAction, ChaosBladeAction, SQLAction, TransactionAction
from .action_validator import ValidationDecision, ValidationIssue, ValidationResult


class SafetyChecker:
    def __init__(
        self,
        *,
        target_database: str,
        allowed_command_roots: Iterable[str | Path] = (),
        max_duration_sec: int = 300,
    ) -> None:
        self.target_database = target_database
        self.allowed_command_roots = tuple(Path(root).expanduser().resolve() for root in allowed_command_roots)
        self.max_duration_sec = max_duration_sec

    def check(self, action: ActionBase) -> ValidationResult:
        issues: list[ValidationIssue] = []
        if isinstance(action, (SQLAction, TransactionAction, BenchBaseAction)):
            if action.database.lower() in {"mysql", "information_schema", "performance_schema", "sys"}:
                issues.append(ValidationIssue(code="SYSTEM_DATABASE_FORBIDDEN", field="database", message="system databases cannot be modified or targeted"))
            if action.duration_sec > self.max_duration_sec:
                issues.append(ValidationIssue(code="DURATION_OVER_SAFETY_LIMIT", field="duration_sec", message="action duration exceeds safety limit"))
        if isinstance(action, SQLAction):
            issues.extend(_dangerous_sql_issues(action.sql))
        elif isinstance(action, TransactionAction):
            for actor in action.actors:
                for step in actor.steps:
                    issues.extend(_dangerous_sql_issues(step.sql))
        elif isinstance(action, BenchBaseAction):
            for field in ("jar_path", "config_path", "results_dir"):
                if not self._path_allowed(Path(getattr(action, field))):
                    issues.append(ValidationIssue(code="PATH_NOT_ALLOWED", field=field, message="path is outside the allowed filesystem roots"))
        elif isinstance(action, ChaosBladeAction):
            if not self._path_allowed(Path(action.blade_path)):
                issues.append(ValidationIssue(code="PATH_NOT_ALLOWED", field="blade_path", message="ChaosBlade binary is outside allowed roots"))
            issues.extend(self._chaos_issues(action))

        return ValidationResult(
            decision=ValidationDecision.REJECT if issues else ValidationDecision.PASS,
            action_id=action.action_id,
            issues=issues,
            checked_fields=["database", "duration_sec", "command" if isinstance(action, ChaosBladeAction) else "sql"],
        )

    def _chaos_issues(self, action: ChaosBladeAction) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        command = action.command.strip()
        if any(token in command for token in (";", "&&", "||", "|", "`", "$(", ">", "<")):
            issues.append(ValidationIssue(code="SHELL_CONTROL_SYNTAX", field="command", message="command contains shell control syntax"))
            return issues
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            return [ValidationIssue(code="COMMAND_PARSE_ERROR", field="command", message=str(exc))]
        allowed = {
            "cpu": {("cpu", "load"), ("cpu", "fullload")},
            "memory": {("mem", "load")},
            "disk": {("disk", "fill"), ("disk", "load")},
            "network": {("network", "delay"), ("network", "loss"), ("network", "packetloss")},
        }
        if tuple(tokens[:2]) not in allowed[action.resource]:
            issues.append(ValidationIssue(code="COMMAND_NOT_WHITELISTED", field="command", message="ChaosBlade command is not in the resource whitelist"))
        return issues

    def _path_allowed(self, path: Path) -> bool:
        if not self.allowed_command_roots:
            return path.is_absolute() or not str(path).startswith("/")
        candidate = path.expanduser().resolve()
        return any(candidate == root or root in candidate.parents for root in self.allowed_command_roots)


def _dangerous_sql_issues(sql: str) -> list[ValidationIssue]:
    normalized = re.sub(r"\s+", " ", sql.strip()).lower()
    patterns = [
        (r"\bdrop\s+database\b", "DROP_DATABASE_FORBIDDEN", "DROP DATABASE is forbidden"),
        (r"\b(drop|truncate)\s+", "DESTRUCTIVE_DDL_FORBIDDEN", "destructive DDL is forbidden"),
        (r"\b(set\s+global|shutdown|grant|revoke|create\s+user|alter\s+user)\b", "SYSTEM_OPERATION_FORBIDDEN", "system operation is forbidden"),
        (r"\binto\s+(outfile|dumpfile)\b", "FILE_EXPORT_FORBIDDEN", "file export is forbidden"),
    ]
    return [ValidationIssue(code=code, field="sql", message=message) for pattern, code, message in patterns if re.search(pattern, normalized)]
