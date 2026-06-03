from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.prompting import PromptTemplateLoader
from agent_runtime.types import EvaluationResult, ReflectionResult


class SelfReflectionAgent:
    def __init__(self, llm_client: ResponsesAPIClient):
        self.llm_client = llm_client
        self.prompt_loader = PromptTemplateLoader()

    def reflect(self, payload: dict) -> ReflectionResult:
        evaluation = payload.get("evaluation_result")
        if isinstance(evaluation, EvaluationResult):
            evaluation_payload = asdict(evaluation)
        else:
            evaluation_payload = evaluation or {}
        if self.llm_client.available():
            prompt_payload = {**payload, "evaluation_result": evaluation_payload}
            system_prompt, user_prompt = self.prompt_loader.render_chat_prompt(
                "reflexion/failure_analysis.md",
                {"CONTEXT_JSON": json.dumps(prompt_payload, ensure_ascii=True, default=str)},
            )
            result = self.llm_client.generate_json(
                system_prompt,
                user_prompt,
                0.0,
            )
            if not result.used_fallback and result.text:
                data = _parse_json_payload(result.text)
                if data:
                    data = _normalize_reflection_payload(data)
                    return ReflectionResult(
                        failure_reason=_as_list(data.get("failure_reason")),
                        suggested_changes=_as_list(data.get("suggested_changes")),
                        task_parameter_updates=_as_update_map(data.get("task_parameter_updates")),
                        agent_specific_feedback=_as_feedback_map(data.get("agent_specific_feedback")),
                        risk_warning=_as_list(data.get("risk_warning")),
                        memory_update=_as_list(data.get("memory_update")),
                        raw_text=result.text,
                    )
        return self._fallback_reflection(evaluation_payload)

    @staticmethod
    def _fallback_reflection(evaluation: dict) -> ReflectionResult:
        reward = evaluation.get("reward", {}) if isinstance(evaluation, dict) else {}
        final_score = reward.get("final_score", 0.0)
        reasons = ["reward score below threshold"] if final_score < 0.7 else ["evaluation did not meet requested chain criteria"]
        suggestions = [
            "increase background workload intensity",
            "extend anomaly overlap with the probe window",
            "prefer lock and resource anomalies when QPS degradation is weak",
        ]
        return ReflectionResult(
            failure_reason=reasons,
            suggested_changes=suggestions,
            task_parameter_updates={},
            agent_specific_feedback={},
            risk_warning=[],
            memory_update=[f"Attempt scored {final_score}; stronger overlap or concurrency may be needed."],
            raw_text="fallback reflection",
        )


def _parse_json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_reflection_payload(data: dict[str, Any]) -> dict[str, Any]:
    if "reflection" in data and isinstance(data["reflection"], dict):
        merged = dict(data["reflection"])
        for key, value in data.items():
            merged.setdefault(key, value)
        data = merged
    if not data.get("failure_reason"):
        data["failure_reason"] = data.get("why_it_failed") or data.get("primary_failure_reasons") or data.get("failure_analysis")
    if not data.get("suggested_changes"):
        data["suggested_changes"] = data.get("recommendations") or data.get("next_attempt") or data.get("parameter_changes")
    if not data.get("memory_update"):
        data["memory_update"] = data.get("lessons") or data.get("memory_items")
    if not data.get("task_parameter_updates"):
        data["task_parameter_updates"] = _infer_task_updates(data)
    return data


def _infer_task_updates(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    text = json.dumps(data, ensure_ascii=True).lower()
    updates: dict[str, dict[str, Any]] = {}
    if "backup" in text or "orders_backup" in text or "order_line" in text:
        backup_updates: dict[str, Any] = {
            "concurrent_with_probe": True,
            "background_duration_seconds": 20,
        }
        if "order_line" in text or "wrong table" in text or "too short" in text or "small table" in text:
            backup_updates["source_table"] = "order_line"
            backup_updates["backup_table"] = "order_line_backup_agent"
        updates["database_table_backup"] = backup_updates
    return updates


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                items.append(json.dumps(item, ensure_ascii=True))
            else:
                items.append(str(item))
        return items
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=True)]
    if value:
        return [str(value)]
    return []


def _as_update_map(value) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, updates in value.items():
        if isinstance(updates, dict):
            normalized[str(key)] = updates
    return normalized


def _as_feedback_map(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _as_list(items) for key, items in value.items()}
