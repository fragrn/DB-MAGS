from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, List

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skills.base import Skill
from agent_runtime.types import DBContextSummary, DBTableProfile, TaskAgentInput


@dataclass
class SQLCandidate:
    sql: str
    purpose: str = ""
    expected_effect: str = ""
    risk: str = ""
    required_transaction_mode: str = ""
    validation_hint: str = ""
    source: str = "llm"
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = payload["metadata"] or {}
        return payload


class GenerateSQLCandidateSkill(Skill):
    name = "generate_sql_candidate_skill"

    def __init__(self, llm_client: ResponsesAPIClient, temperature: float):
        self.llm_client = llm_client
        self.temperature = temperature

    def execute(self, anomaly_type: str, db_context: DBContextSummary) -> List[str]:
        return [candidate["sql"] for candidate in self.execute_structured("slow_sql", anomaly_type, anomaly_type, db_context)]

    def execute_structured(
        self,
        agent_name: str,
        anomaly_type: str,
        subgoal: str,
        db_context: DBContextSummary,
        task_input: TaskAgentInput | None = None,
        constraints: dict[str, Any] | None = None,
        candidate_count: int = 5,
    ) -> List[dict[str, Any]]:
        llm_candidates = self._llm_candidates(agent_name, anomaly_type, subgoal, db_context, task_input, constraints or {}, candidate_count)
        if llm_candidates:
            return [candidate.to_dict() for candidate in llm_candidates]
        return [
            SQLCandidate(sql=sql, purpose="emergency fallback candidate", source="static_fallback", metadata={"used_static_fallback": True}).to_dict()
            for sql in self._fallback_candidates(anomaly_type, db_context)
        ]

    def _llm_candidates(
        self,
        agent_name: str,
        anomaly_type: str,
        subgoal: str,
        db_context: DBContextSummary,
        task_input: TaskAgentInput | None,
        constraints: dict[str, Any],
        candidate_count: int,
    ) -> List[SQLCandidate]:
        if not self.llm_client.available():
            return []
        schema_summary = self._schema_summary(db_context)
        memory = task_input.memory if task_input and isinstance(task_input.memory, dict) else {}
        global_context = task_input.global_context if task_input else {}
        user_prompt = json.dumps(
            {
                "agent_name": agent_name,
                "anomaly_type": anomaly_type,
                "subgoal": subgoal,
                "database": db_context.database,
                "tables": schema_summary,
                "target_chain": global_context.get("target_chain", []),
                "planner_parameters": global_context.get("planner_parameters", {}),
                "latest_reflection": memory.get("latest_reflection", {}),
                "short_term_trace_summary": self._short_term_summary(memory.get("short_term_trace", [])),
                "support_tables": [
                    "agent_order_by_support",
                    "agent_group_by_support",
                    "agent_large_scan_support",
                    "agent_implicit_conversion_support",
                    "agent_excessive_index",
                ],
                "candidate_count": candidate_count,
                "agent_instructions": self._agent_instructions(agent_name, anomaly_type),
                "constraints": [
                    "Do not use DROP, DELETE, TRUNCATE, GRANT, REVOKE.",
                    "Do not modify mysql, information_schema, performance_schema, or sys schemas.",
                    "Do not produce UPDATE without WHERE.",
                    "Use only listed tables and columns.",
                    "Return diverse candidates, not minor text variations.",
                    "Each candidate must be one MySQL statement.",
                    *list(constraints.get("sql_constraints", [])),
                ],
                "return_schema": {
                    "candidates": [
                        {
                            "sql": "single SQL statement",
                            "purpose": "why this candidate matches the anomaly",
                            "expected_effect": "metric or DB behavior expected to change",
                            "risk": "low|medium|high",
                            "required_transaction_mode": "autocommit|explicit_transaction|lock_holder|read_only",
                            "validation_hint": "what EXPLAIN/probe should show",
                            "source_table": "optional source table for backup tasks",
                            "backup_table": "optional backup table for backup tasks",
                        }
                    ]
                },
            },
            ensure_ascii=True,
        )
        system_prompt = (
            "You are a MySQL anomaly SQL generator for a safe experiment environment. Return JSON only. "
            "Generate diverse candidate SQL for the specific task agent and anomaly. Do not execute anything. "
            "The local agent will validate and may reject every candidate."
        )
        result = self.llm_client.generate_json(system_prompt, user_prompt, self.temperature)
        if result.used_fallback or not result.text:
            return []
        try:
            payload = json.loads(result.text)
            raw_candidates = payload.get("candidates", payload if isinstance(payload, list) else [])
            return self._normalize_candidates(raw_candidates)
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _normalize_candidates(raw_candidates: object) -> List[SQLCandidate]:
        if not isinstance(raw_candidates, list):
            return []
        candidates: List[SQLCandidate] = []
        for item in raw_candidates:
            if isinstance(item, str):
                sql = item.strip()
                metadata: dict[str, Any] = {}
                purpose = ""
                expected_effect = ""
                risk = ""
                tx_mode = ""
                hint = ""
            elif isinstance(item, dict):
                sql = str(item.get("sql", "")).strip()
                metadata = {key: value for key, value in item.items() if key not in {"sql", "purpose", "expected_effect", "risk", "required_transaction_mode", "validation_hint"}}
                purpose = str(item.get("purpose", ""))
                expected_effect = str(item.get("expected_effect", ""))
                risk = str(item.get("risk", ""))
                tx_mode = str(item.get("required_transaction_mode", ""))
                hint = str(item.get("validation_hint", ""))
            else:
                continue
            if sql:
                candidates.append(
                    SQLCandidate(
                        sql=sql,
                        purpose=purpose,
                        expected_effect=expected_effect,
                        risk=risk,
                        required_transaction_mode=tx_mode,
                        validation_hint=hint,
                        metadata=metadata,
                    )
                )
        return candidates

    @staticmethod
    def _schema_summary(db_context: DBContextSummary) -> list[dict[str, Any]]:
        schema_summary = []
        for table in db_context.tables[:15]:
            cols = [f"{col.name}:{col.data_type}:{'idx' if col.indexed else 'noidx'}" for col in table.columns[:12]]
            schema_summary.append({"table": table.name, "row_count": table.row_count, "columns": cols, "indexes": table.indexes})
        return schema_summary

    @staticmethod
    def _short_term_summary(trace: object) -> list[dict[str, Any]]:
        if not isinstance(trace, list):
            return []
        summary = []
        for item in trace[-3:]:
            if not isinstance(item, dict):
                continue
            summary.append(
                {
                    "round": item.get("round"),
                    "task_agent_outputs": item.get("task_agent_outputs", []),
                    "baseline_metrics": item.get("baseline_metrics", {}),
                    "after_metrics": item.get("after_metrics", {}),
                    "evaluation": item.get("evaluation", {}),
                    "reflection": item.get("reflection", {}),
                }
            )
        return summary

    @staticmethod
    def _agent_instructions(agent_name: str, anomaly_type: str) -> list[str]:
        if agent_name == "slow_sql":
            return [
                "Generate SQL that can cause slow queries: missing index filters, large scans, joins, GROUP BY, ORDER BY, or implicit conversions.",
                "Prefer read-only SELECT unless the anomaly specifically needs UPDATE.",
                "Explain why EXPLAIN should show scan-heavy or high-row work.",
            ]
        if agent_name == "lock_conflict":
            return [
                "Generate lock holder or waiter SQL for the requested lock anomaly.",
                "For record_lock prefer SELECT ... FOR UPDATE or UPDATE ... WHERE using a selective predicate.",
                "For table_lock use LOCK TABLES on an allowed table.",
                "For metadata_lock use a safe statement that can hold metadata locks; avoid irreversible DDL.",
            ]
        if agent_name == "traffic_surge":
            return [
                "Generate SQL suitable for high-frequency concurrent execution.",
                "Prefer bounded SELECT statements that still create measurable database pressure.",
            ]
        if agent_name == "database_backup":
            return [
                "Suggest a source table and backup table that would create observable backup interference.",
                "If you include SQL, use CREATE TABLE backup AS SELECT * FROM source; the local agent will regenerate rollback-safe SQL.",
            ]
        return [f"Generate candidates for {agent_name}:{anomaly_type}."]

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
            if not candidates:
                candidates.append(f"SELECT COUNT(*) FROM {table.name}")
        return candidates
