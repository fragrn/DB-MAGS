from __future__ import annotations

import json
from typing import List

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skills.base import Skill
from agent_runtime.types import DBContextSummary, DBTableProfile


class GenerateSQLCandidateSkill(Skill):
    name = "generate_sql_candidate_skill"

    def __init__(self, llm_client: ResponsesAPIClient, temperature: float):
        self.llm_client = llm_client
        self.temperature = temperature

    def execute(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        fallback = self._fallback_candidates(anomaly_type, db_context)
        llm_candidates = self._llm_candidates(anomaly_type, db_context)
        merged = []
        for candidate in llm_candidates + fallback:
            normalized = candidate.strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
        return merged

    def _llm_candidates(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        if not self.llm_client.available():
            return []
        schema_summary = []
        for table in db_context.tables[:10]:
            cols = [f"{col.name}:{col.data_type}:{'idx' if col.indexed else 'noidx'}" for col in table.columns[:10]]
            schema_summary.append({"table": table.name, "row_count": table.row_count, "columns": cols})
        system_prompt = (
            "You generate MySQL anomaly candidates. Return JSON with a key named candidates. "
            "Each candidate must be a single SQL statement. Prefer SELECT/UPDATE/LOCK/ALTER/CREATE TABLE only. "
            "Use only tables listed in the schema summary or known agent support tables."
        )
        user_prompt = json.dumps(
            {
                "anomaly_type": anomaly_type,
                "database": db_context.database,
                "tables": schema_summary,
                "support_tables": [
                    "agent_order_by_support",
                    "agent_group_by_support",
                    "agent_large_scan_support",
                    "agent_implicit_conversion_support",
                    "agent_excessive_index",
                ],
                "constraints": [
                    "Do not use DROP, DELETE, TRUNCATE, GRANT, REVOKE.",
                    "Prefer queries that clearly exhibit the target anomaly.",
                    "For lock anomalies, produce a single statement that can hold or trigger the lock.",
                    "For order_by/group_by/large_table_scan anomalies, prefer dedicated support tables.",
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

    def _fallback_candidates(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        if anomaly_type == "missing_index":
            return self._missing_index_candidates(db_context)
        if anomaly_type == "excessive_index":
            return ["UPDATE agent_excessive_index SET o_ol_cnt = o_ol_cnt + 1 WHERE o_c_id > 1000"]
        if anomaly_type == "implicit_conversion":
            return [
                "SELECT COUNT(*) FROM agent_implicit_conversion_support WHERE customer_id_varchar = 12345",
                "SELECT * FROM agent_implicit_conversion_support WHERE amount_text > 1000",
            ]
        if anomaly_type == "multi_table_join":
            return [
                "SELECT COUNT(*) FROM orders o JOIN order_line ol ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id JOIN customer c ON c.c_w_id = o.o_w_id AND c.c_d_id = o.o_d_id AND c.c_id = o.o_c_id WHERE o.o_carrier_id IS NOT NULL",
            ]
        if anomaly_type == "order_by":
            return ["SELECT * FROM agent_order_by_support ORDER BY ol_amount DESC, ol_dist_info ASC LIMIT 5000"]
        if anomaly_type == "group_by":
            return ["SELECT ol_i_id, SUM(ol_amount) FROM agent_group_by_support GROUP BY ol_i_id ORDER BY SUM(ol_amount) DESC"]
        if anomaly_type == "large_table_scan":
            return ["SELECT COUNT(*) FROM agent_large_scan_support WHERE c_last IS NOT NULL AND c_credit IN ('BC', 'GC')"]
        if anomaly_type == "record_lock":
            return ["UPDATE new_orders SET no_o_id = no_o_id + 0 WHERE no_w_id = 1 AND no_d_id = 1 AND no_o_id > 10"]
        if anomaly_type == "table_lock":
            return ["LOCK TABLES new_orders WRITE"]
        if anomaly_type == "metadata_lock":
            return ["ALTER TABLE new_orders ADD COLUMN agent_meta_lock_run CHAR(2)"]
        return self._missing_index_candidates(db_context)

    @staticmethod
    def _missing_index_candidates(db_context: DBContextSummary) -> List[str]:
        ranked_tables = sorted(db_context.tables, key=lambda item: item.row_count or 0, reverse=True)
        candidates: List[str] = []
        for table in ranked_tables[:8]:
            for column in table.columns:
                if column.indexed:
                    continue
                if column.data_type not in {"int", "bigint", "smallint", "tinyint", "decimal", "float", "double"}:
                    continue
                threshold = 100 if (table.row_count or 0) < 10000 else 1000
                candidates.append(f"SELECT COUNT(*) FROM {table.name} WHERE {column.name} > {threshold}")
                if len(candidates) >= 4:
                    return candidates
        if ranked_tables:
            table = ranked_tables[0]
            for column in table.columns[:2]:
                candidates.append(f"SELECT COUNT(*) FROM {table.name} WHERE {column.name} IS NOT NULL")
        return candidates
