from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict

from agent_runtime.config import RuntimeConfig

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    raw: Dict[str, Any]
    used_fallback: bool = False
    error_type: str = ""
    error_message: str = ""
    transport_used: str = ""


class ResponsesAPIClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.openai_api_key) and self.config.planner_enabled

    def generate_json(self, system_prompt: str, user_prompt: str, temperature: float) -> LLMResult:
        if not self.available():
            message = "LLM unavailable: missing API key or planner disabled."
            logger.warning(message)
            return LLMResult(
                text="",
                raw={},
                used_fallback=True,
                error_type="unavailable",
                error_message=message,
            )

        primary_mode = self._primary_mode()
        if primary_mode == "chat_completions":
            result = self._send_chat_request(system_prompt, user_prompt, temperature)
            if not result.used_fallback:
                return result
            if self._should_try_secondary(result):
                logger.warning(
                    "LLM chat_completions request failed (%s); falling back to responses.",
                    result.error_message or result.error_type or "unknown error",
                )
                retry = self._send_responses_request(system_prompt, user_prompt, temperature)
                if not retry.used_fallback:
                    return retry
                return retry
            return result

        result = self._send_responses_request(system_prompt, user_prompt, temperature)
        if not result.used_fallback:
            return result
        if self._should_try_secondary(result):
            logger.warning(
                "LLM responses request failed (%s); falling back to chat_completions.",
                result.error_message or result.error_type or "unknown error",
            )
            retry = self._send_chat_request(system_prompt, user_prompt, temperature)
            if not retry.used_fallback:
                return retry
            return retry
        return result

    def _send_responses_request(self, system_prompt: str, user_prompt: str, temperature: float) -> LLMResult:
        payload = {
            "model": self.config.openai_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "text": {"format": {"type": "json_object"}},
            "reasoning": {"effort": "medium"},
        }
        if self._supports_temperature(self.config.openai_model):
            payload["temperature"] = temperature
        result = self._send_request("/responses", payload, transport="responses", extract_text=self._extract_responses_text)
        if not result.used_fallback:
            return result
        if self._is_unsupported_temperature_result(result) and "temperature" in payload:
            logger.info("Retrying responses request without temperature for model %s", self.config.openai_model)
            retry_payload = dict(payload)
            retry_payload.pop("temperature", None)
            return self._send_request("/responses", retry_payload, transport="responses", extract_text=self._extract_responses_text)
        return result

    def _send_chat_request(self, system_prompt: str, user_prompt: str, temperature: float) -> LLMResult:
        payload = {
            "model": self.config.openai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        return self._send_request(
            "/chat/completions",
            payload,
            transport="chat_completions",
            extract_text=self._extract_chat_text,
        )

    def _send_request(self, endpoint: str, payload: Dict[str, Any], transport: str, extract_text) -> LLMResult:
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
            raw_body = self._read_http_error(exc)
            message = self._extract_error_message(raw_body) or f"HTTP {exc.code} {exc.reason}"
            logger.warning("LLM %s request failed with HTTPError %s: %s", transport, exc.code, message)
            return LLMResult(
                text="",
                raw=raw_body,
                used_fallback=True,
                error_type="http_error",
                error_message=message,
                transport_used=transport,
            )
        except urllib.error.URLError as exc:
            message = repr(exc)
            logger.warning("LLM %s request failed with URLError: %s", transport, message)
            return LLMResult(
                text="",
                raw={},
                used_fallback=True,
                error_type="url_error",
                error_message=message,
                transport_used=transport,
            )
        except TimeoutError as exc:
            message = repr(exc)
            logger.warning("LLM %s request timed out: %s", transport, message)
            return LLMResult(
                text="",
                raw={},
                used_fallback=True,
                error_type="timeout",
                error_message=message,
                transport_used=transport,
            )
        except json.JSONDecodeError as exc:
            message = f"Failed to decode LLM response JSON: {exc}"
            logger.warning("LLM %s response decode failed: %s", transport, message)
            return LLMResult(
                text="",
                raw={},
                used_fallback=True,
                error_type="json_decode",
                error_message=message,
                transport_used=transport,
            )
        text = self._normalize_text(extract_text(body))
        return LLMResult(text=text, raw=body, used_fallback=False, transport_used=transport)

    def _primary_mode(self) -> str:
        mode = getattr(self.config, "openai_api_mode", "chat_completions") or "chat_completions"
        normalized = mode.strip().lower()
        if normalized in {"chat", "chat_completion", "chat_completions"}:
            return "chat_completions"
        if normalized == "responses":
            return "responses"
        if normalized == "auto":
            return "chat_completions"
        return "chat_completions"

    def _should_try_secondary(self, result: LLMResult) -> bool:
        mode = getattr(self.config, "openai_api_mode", "chat_completions") or "chat_completions"
        if mode.strip().lower() != "auto":
            return False
        if result.error_type in {"url_error", "timeout"}:
            return True
        if result.error_type != "http_error":
            return False
        message = (result.error_message or "").lower()
        return any(
            token in message
            for token in (
                "429",
                "too many requests",
                "负载已饱和",
                "not found",
                "404",
                "unsupported",
                "not support",
                "invalid endpoint",
                "unrecognized request",
                "5xx",
                "bad gateway",
                "service unavailable",
            )
        )

    @staticmethod
    def _extract_responses_text(body: Dict[str, Any]) -> str:
        if isinstance(body.get("output_text"), str):
            return body["output_text"]
        output = body.get("output", [])
        chunks = []
        for item in output:
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks)

    @staticmethod
    def _extract_chat_text(body: Dict[str, Any]) -> str:
        choices = body.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            return "\n".join(parts)
        return ""

    @staticmethod
    def _normalize_text(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            lines = cleaned.splitlines()
            if len(lines) >= 3:
                cleaned = "\n".join(lines[1:-1]).strip()
        return cleaned

    @staticmethod
    def _supports_temperature(model: str) -> bool:
        lowered = model.lower()
        return not lowered.startswith("gpt-5")

    @classmethod
    def _is_unsupported_temperature_result(cls, result: LLMResult) -> bool:
        if result.error_type != "http_error":
            return False
        message = (result.error_message or "").lower()
        return "temperature" in message and "not supported" in message

    @staticmethod
    def _read_http_error(exc: urllib.error.HTTPError) -> Dict[str, Any]:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw_body": body}

    @staticmethod
    def _extract_error_message(body: Dict[str, Any]) -> str:
        if not isinstance(body, dict):
            return ""
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
        if body.get("message"):
            return str(body["message"])
        if body.get("raw_body"):
            return str(body["raw_body"])
        return ""
