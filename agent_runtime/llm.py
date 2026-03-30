from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent_runtime.config import RuntimeConfig


@dataclass
class LLMResult:
    text: str
    raw: Dict[str, Any]
    used_fallback: bool = False


class ResponsesAPIClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.openai_api_key) and self.config.planner_enabled

    def generate_json(self, system_prompt: str, user_prompt: str, temperature: float) -> LLMResult:
        if not self.available():
            return LLMResult(text="", raw={}, used_fallback=True)

        payload = {
            "model": self.config.openai_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "text": {"format": {"type": "json_object"}},
            "reasoning": {"effort": "medium"},
            "temperature": temperature,
        }
        req = urllib.request.Request(
            f"{self.config.openai_base_url.rstrip('/')}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return LLMResult(text="", raw={}, used_fallback=True)

        text = self._extract_text(body)
        return LLMResult(text=text, raw=body, used_fallback=False)

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> str:
        if isinstance(body.get("output_text"), str):
            return body["output_text"]
        output = body.get("output", [])
        chunks = []
        for item in output:
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks)
