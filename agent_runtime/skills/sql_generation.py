from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, List

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.prompting import PromptTemplateLoader
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
        self.prompt_loader = PromptTemplateLoader()

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
        return [candidate.to_dict() for candidate in llm_candidates]

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
        prompt_payload = {
            "agent_name": agent_name,
            "anomaly_type": anomaly_type,
            "subgoal": subgoal,
            "database": db_context.database,
            "tables": schema_summary,
            "target_chain": global_context.get("target_chain", []),
            "planner_parameters": global_context.get("planner_parameters", {}),
            "latest_reflection": memory.get("latest_reflection", {}),
            "short_term_trace_summary": self._short_term_summary(memory.get("short_term_trace", [])),
            "long_term_memory": memory.get("long_term_memory", []),
            "support_tables": [
                "agent_order_by_support",
                "agent_group_by_support",
                "agent_large_scan_support",
                "agent_implicit_conversion_support",
                "agent_excessive_index",
            ],
            "candidate_count": candidate_count,
            "constraints": [
                "Do not use DROP, DELETE, TRUNCATE, GRANT, REVOKE.",
                "Do not modify mysql, information_schema, performance_schema, or sys schemas.",
                "Do not produce UPDATE without WHERE.",
                "Use only listed tables and columns.",
                "Return diverse candidates, not minor text variations.",
                "Each candidate must be one MySQL statement.",
                *list(constraints.get("sql_constraints", [])),
            ],
        }
        return_schema = {
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
        }
        system_prompt, user_prompt = self.prompt_loader.render_chat_prompt(
            f"task_agents/{self._prompt_name(agent_name)}.md",
            {
                "CONTEXT_JSON": json.dumps(prompt_payload, ensure_ascii=True),
                "RETURN_SCHEMA_JSON": json.dumps(return_schema, ensure_ascii=True),
            },
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
    def _prompt_name(agent_name: str) -> str:
        aliases = {"database_backup": "database_backup"}
        return aliases.get(agent_name, agent_name)
