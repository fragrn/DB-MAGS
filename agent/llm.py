from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent.config import RuntimeConfig


@dataclass
class LLMCallResult:
    text: str
    raw: Dict[str, Any]
    error: Optional[str] = None
    endpoint: Optional[str] = None


class ResponsesAPIClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.openai_api_key) and self.config.planner_enabled

    def summarize_plan(self, profile_summary: Dict[str, Any], plan_summary: Dict[str, Any]) -> LLMCallResult:
        if not self.available():
            return LLMCallResult(text="", raw={}, error="openai_not_configured")

        system_prompt = (
            "You are helping summarize a database anomaly experiment plan. "
            "Provide a short, practical summary in plain English with the main goal, "
            "selected anomalies, and the expected signals to watch."
        )
        user_payload = {
            "database_profile": profile_summary,
            "plan": plan_summary,
        }

        responses_result = self._call_responses(system_prompt, user_payload)
        if not self._should_fallback_to_chat(responses_result):
            return responses_result
        chat_result = self._call_chat_completions(system_prompt, user_payload)
        if chat_result.text or not chat_result.error:
            return chat_result
        combined_error = f"responses_failed: {responses_result.error}; chat_failed: {chat_result.error}"
        return LLMCallResult(text="", raw={}, error=combined_error, endpoint="fallback")

    def _call_responses(self, system_prompt: str, user_payload: Dict[str, Any]) -> LLMCallResult:
        payload = {
            "model": self.config.openai_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=True)}]},
            ],
            "reasoning": {"effort": "low"},
        }
        return self._post_json("/v1/responses", payload, parser=self._extract_responses_text)

    def _call_chat_completions(self, system_prompt: str, user_payload: Dict[str, Any]) -> LLMCallResult:
        payload = {
            "model": self.config.openai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
            ],
            "temperature": 0.2,
        }
        return self._post_json("/v1/chat/completions", payload, parser=self._extract_chat_text)

    def _post_json(self, endpoint: str, payload: Dict[str, Any], parser) -> LLMCallResult:
        req = urllib.request.Request(
            f"{self.config.openai_base_url.rstrip('/')}{endpoint}",
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
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return LLMCallResult(text="", raw={}, error=f"http_{exc.code}: {detail}", endpoint=endpoint)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return LLMCallResult(text="", raw={}, error=str(exc), endpoint=endpoint)

        return LLMCallResult(text=parser(body), raw=body, endpoint=endpoint)

    @staticmethod
    def _should_fallback_to_chat(result: LLMCallResult) -> bool:
        if result.text:
            return False
        if not result.error:
            return True
        fallback_signals = (
            "unsupported",
            "not found",
            "invalid url",
            "unknown path",
            "unrecognized request argument",
            "responses_failed",
            "does not exist",
            "chat/completions",
            "response format",
        )
        error_lower = result.error.lower()
        return any(signal in error_lower for signal in fallback_signals)

    @staticmethod
    def _extract_responses_text(body: Dict[str, Any]) -> str:
        if isinstance(body.get("output_text"), str):
            return body["output_text"]
        chunks = []
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks)

    @staticmethod
    def _extract_chat_text(body: Dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"} and item.get("text"):
                    parts.append(item["text"])
            return "\n".join(parts)
        return ""
