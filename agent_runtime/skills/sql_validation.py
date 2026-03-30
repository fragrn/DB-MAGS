from __future__ import annotations

import re
from typing import Dict, List

from agent_runtime.skills.base import Skill
from agent_runtime.utils import contains_dangerous_sql


class ValidateSQLSkill(Skill):
    name = "validate_sql_skill"

    def execute(self, sql: str, allowed_tables: List[str], anomaly_type: str) -> Dict[str, object]:
        stripped = sql.strip().rstrip(";")
        lowered = stripped.lower()
        errors = []
        if contains_dangerous_sql(stripped):
            errors.append("contains dangerous keyword")
        if not any(lowered.startswith(prefix) for prefix in ("select", "update", "lock", "alter", "explain")):
            errors.append("statement type is not on the allow-list")
        referenced_tables = re.findall(r"(?:from|join|update|table|lock tables)\s+([a-zA-Z0-9_\.]+)", lowered)
        normalized_allowed = {table.lower() for table in allowed_tables}
        for table in referenced_tables:
            raw = table.split(",")[0]
            normalized = raw.split(".")[-1]
            if normalized_allowed and normalized not in normalized_allowed:
                errors.append(f"references unknown table: {raw}")
        if anomaly_type != "metadata_lock" and lowered.startswith("alter"):
            errors.append("ALTER is reserved for metadata_lock")
        return {"valid": not errors, "errors": errors, "sql": stripped + ";"}
