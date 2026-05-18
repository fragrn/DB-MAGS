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


class ResponsesAPIClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.openai_api_key) and self.config.planner_enabled

    def generate_json(self, system_prompt: str, user_prompt: str, temperature: float) -> LLMResult:
        if not self.available():
            message = "LLM unavailable: missing API key or planner disabled."
            logger.warning(message)
            return LLMResult(text="", raw={}, used_fallback=True, error_type="unavailable", error_message=message)

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
        except urllib.error.HTTPError as exc:
            raw_body = self._read_http_error(exc)
            message = self._extract_error_message(raw_body) or f"HTTP {exc.code} {exc.reason}"
            logger.warning("LLM request failed with HTTPError %s: %s", exc.code, message)
            if self._is_unsupported_temperature_error(raw_body):
                logger.info("Retrying LLM request without temperature for model %s", self.config.openai_model)
                retry_payload = dict(payload)
                retry_payload.pop("temperature", None)
                retry_result = self._send_request(retry_payload)
                if not retry_result.used_fallback:
                    return retry_result
                retry_message = retry_result.error_message or "retry without temperature still failed"
                logger.warning("LLM retry without temperature failed: %s", retry_message)
                return retry_result
            return LLMResult(
                text="",
                raw=raw_body,
                used_fallback=True,
                error_type="http_error",
                error_message=message,
            )
        except urllib.error.URLError as exc:
            message = repr(exc)
            logger.warning("LLM request failed with URLError: %s", message)
            return LLMResult(text="", raw={}, used_fallback=True, error_type="url_error", error_message=message)
        except TimeoutError as exc:
            message = repr(exc)
            logger.warning("LLM request timed out: %s", message)
            return LLMResult(text="", raw={}, used_fallback=True, error_type="timeout", error_message=message)
        except json.JSONDecodeError as exc:
            message = f"Failed to decode LLM response JSON: {exc}"
            logger.warning(message)
            return LLMResult(text="", raw={}, used_fallback=True, error_type="json_decode", error_message=message)

        text = self._extract_text(body)
        return LLMResult(text=text, raw=body, used_fallback=False)

    def _send_request(self, payload: Dict[str, Any]) -> LLMResult:
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
        except urllib.error.HTTPError as exc:
            raw_body = self._read_http_error(exc)
            message = self._extract_error_message(raw_body) or f"HTTP {exc.code} {exc.reason}"
            return LLMResult(text="", raw=raw_body, used_fallback=True, error_type="http_error", error_message=message)
        except urllib.error.URLError as exc:
            return LLMResult(text="", raw={}, used_fallback=True, error_type="url_error", error_message=repr(exc))
        except TimeoutError as exc:
            return LLMResult(text="", raw={}, used_fallback=True, error_type="timeout", error_message=repr(exc))
        except json.JSONDecodeError as exc:
            return LLMResult(
                text="",
                raw={},
                used_fallback=True,
                error_type="json_decode",
                error_message=f"Failed to decode LLM response JSON: {exc}",
            )
        return LLMResult(text=self._extract_text(body), raw=body, used_fallback=False)

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

    @staticmethod
    def _supports_temperature(model: str) -> bool:
        lowered = model.lower()
        return not lowered.startswith("gpt-5")

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
        if body.get("raw_body"):
            return str(body["raw_body"])
        return ""

    @classmethod
    def _is_unsupported_temperature_error(cls, body: Dict[str, Any]) -> bool:
        message = cls._extract_error_message(body).lower()
        return "temperature" in message and "not supported" in message
