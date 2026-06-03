from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, List

from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.prompting import PromptTemplateLoader
from agent_runtime.skills.base import Skill
from agent_runtime.types import TaskAgentInput


@dataclass
class ResourceCandidate:
    resource_type: str
    intensity: str = ""
    duration_seconds: int = 0
    purpose: str = ""
    expected_effect: str = ""
    risk: str = ""
    validation_hint: str = ""
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GenerateResourceCandidateSkill(Skill):
    name = "generate_resource_candidate_skill"

    def __init__(self, llm_client: ResponsesAPIClient, temperature: float):
        self.llm_client = llm_client
        self.temperature = temperature
        self.prompt_loader = PromptTemplateLoader()

    def execute(
        self,
        anomaly_type: str,
        task_input: TaskAgentInput | None = None,
        os_metrics: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        candidate_count: int = 3,
    ) -> List[dict[str, Any]]:
        if not self.llm_client.available():
            return []
        memory = task_input.memory if task_input and isinstance(task_input.memory, dict) else {}
        prompt_payload = {
            "agent_name": "resource_bottleneck",
            "anomaly_type": anomaly_type,
            "subgoal": task_input.subgoal if task_input else anomaly_type,
            "environment_snapshot": task_input.environment_snapshot if task_input else {},
            "os_metrics": os_metrics or {},
            "planner_parameters": parameters or {},
            "latest_reflection": memory.get("latest_reflection", {}),
            "short_term_trace": memory.get("short_term_trace", [])[-3:] if isinstance(memory.get("short_term_trace", []), list) else [],
            "long_term_memory": memory.get("long_term_memory", []),
            "candidate_count": candidate_count,
        }
        return_schema = {
            "candidates": [
                {
                    "resource_type": "cpu|io|network|memory|disk",
                    "intensity": "low|medium|high",
                    "duration_seconds": "integer duration within safety constraints",
                    "purpose": "why this candidate matches the subgoal",
                    "expected_effect": "metric or DB behavior expected to change",
                    "risk": "low|medium|high",
                    "validation_hint": "what metric/probe should verify",
                }
            ]
        }
        system_prompt, user_prompt = self.prompt_loader.render_chat_prompt(
            "task_agents/resource_bottleneck.md",
            {
                "CONTEXT_JSON": json.dumps(prompt_payload, ensure_ascii=True, default=str),
                "RETURN_SCHEMA_JSON": json.dumps(return_schema, ensure_ascii=True),
            },
        )
        result = self.llm_client.generate_json(system_prompt, user_prompt, self.temperature)
        if result.used_fallback or not result.text:
            return []
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            return []
        raw = payload.get("candidates", payload if isinstance(payload, list) else [])
        return [candidate.to_dict() for candidate in self._normalize(raw)]

    @staticmethod
    def _normalize(raw_candidates: object) -> List[ResourceCandidate]:
        if not isinstance(raw_candidates, list):
            return []
        candidates: List[ResourceCandidate] = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            resource_type = str(item.get("resource_type", "")).strip()
            if not resource_type:
                continue
            try:
                duration = int(float(item.get("duration_seconds", 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            candidates.append(
                ResourceCandidate(
                    resource_type=resource_type,
                    intensity=str(item.get("intensity", "")),
                    duration_seconds=duration,
                    purpose=str(item.get("purpose", "")),
                    expected_effect=str(item.get("expected_effect", "")),
                    risk=str(item.get("risk", "")),
                    validation_hint=str(item.get("validation_hint", "")),
                )
            )
        return candidates
