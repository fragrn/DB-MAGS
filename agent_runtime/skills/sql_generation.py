from __future__ import annotations

import json
from typing import Dict, List

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skills.base import Skill
from agent_runtime.types import DBContextSummary
from tpcc_operation_set import (
    group_by,
    implicit_conversion,
    lock_slow_query,
    meta_data_sqls,
    missing_index,
    order_by,
    query_whole_table,
    query_with_too_much_join,
    record_lock_sqls,
    table_lock_sqls,
    too_much_index,
)


class GenerateSQLCandidateSkill(Skill):
    name = "generate_sql_candidate_skill"

    def __init__(self, llm_client: ResponsesAPIClient, temperature: float):
        self.llm_client = llm_client
        self.temperature = temperature

    def execute(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        fallback = self._fallback_candidates(anomaly_type, db_context)
        llm_candidates = self._llm_candidates(anomaly_type, db_context)
        return llm_candidates + [sql for sql in fallback if sql not in llm_candidates]

    def _llm_candidates(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        if not self.llm_client.available():
            return []
        schema_summary = []
        for table in db_context.tables[:8]:
            cols = [f"{col.name}:{col.data_type}:{'idx' if col.indexed else 'noidx'}" for col in table.columns[:8]]
            schema_summary.append({"table": table.name, "row_count": table.row_count, "columns": cols})
        system_prompt = (
            "You generate MySQL anomaly candidates. Return JSON with a key named candidates. "
            "Each candidate must be a single SQL statement. Prefer SELECT/UPDATE/LOCK/ALTER only."
        )
        user_prompt = json.dumps(
            {
                "anomaly_type": anomaly_type,
                "database": db_context.database,
                "tables": schema_summary,
                "constraints": [
                    "Prefer statements that can create slow SQL or lock contention",
                    "Do not use DROP, DELETE, TRUNCATE, GRANT, REVOKE",
                    "Use only table and column names present in schema summary",
                ],
            },
            ensure_ascii=True,
        )
        result = self.llm_client.generate_json(system_prompt, user_prompt, self.temperature)
        if result.used_fallback or not result.text:
            return []
        try:
            payload = json.loads(result.text)
            candidates = payload.get("candidates", [])
            return [candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()]
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _fallback_candidates(anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        mapping = {
            "table_lock": table_lock_sqls,
            "metadata_lock": meta_data_sqls,
            "record_lock": lambda: record_lock_sqls(0.8),
            "missing_index": missing_index,
            "too_much_index": too_much_index,
            "implicit_conversion": implicit_conversion,
            "multi_table_join": query_with_too_much_join,
            "order_by": order_by,
            "group_by": group_by,
            "large_table_scan": query_whole_table,
            "lock_slow": lambda: lock_slow_query()[1],
        }
        generator = mapping.get(anomaly_type, missing_index)
        try:
            return list(generator())
        except Exception:
            candidates = []
            for table in db_context.tables:
                non_indexed = [col for col in table.columns if not col.indexed]
                if non_indexed:
                    candidates.append(f"SELECT COUNT(*) FROM {table.name} WHERE {non_indexed[0].name} IS NOT NULL;")
            return candidates
