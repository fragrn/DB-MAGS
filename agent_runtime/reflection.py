from __future__ import annotations

import json
from dataclasses import asdict

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.types import EvaluationResult, ReflectionResult


class SelfReflectionAgent:
    def __init__(self, llm_client: ResponsesAPIClient):
        self.llm_client = llm_client

    def reflect(self, payload: dict) -> ReflectionResult:
        evaluation = payload.get("evaluation_result")
        if isinstance(evaluation, EvaluationResult):
            evaluation_payload = asdict(evaluation)
        else:
            evaluation_payload = evaluation or {}
        if self.llm_client.available():
            result = self.llm_client.generate_json(
                "Return JSON only. Analyze why a database anomaly reproduction attempt failed and suggest concrete changes.",
                json.dumps({**payload, "evaluation_result": evaluation_payload}, ensure_ascii=True, default=str),
                0.0,
            )
            if not result.used_fallback and result.text:
                try:
                    data = json.loads(result.text)
                    return ReflectionResult(
                        failure_reason=_as_list(data.get("failure_reason")),
                        suggested_changes=_as_list(data.get("suggested_changes")),
                        risk_warning=_as_list(data.get("risk_warning")),
                        memory_update=_as_list(data.get("memory_update")),
                        raw_text=result.text,
                    )
                except json.JSONDecodeError:
                    pass
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
            risk_warning=[],
            memory_update=[f"Attempt scored {final_score}; stronger overlap or concurrency may be needed."],
            raw_text="fallback reflection",
        )


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value:
        return [str(value)]
    return []
