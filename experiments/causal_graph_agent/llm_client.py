from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import LLMConfig


@dataclass
class LLMResponse:
    content: str
    raw: dict[str, Any]


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible chat-completions client using stdlib only."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()

    def is_configured(self) -> bool:
        return bool(self.config.api_key and self.config.base_url and self.config.model)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout: int = 60,
    ) -> LLMResponse:
        if not self.is_configured():
            raise RuntimeError("LLM API is not configured. Set OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL in .env.")
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.planner_temperature if temperature is None else temperature,
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM API request failed: {exc.reason}") from exc
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        return LLMResponse(content=content, raw=raw)

    def ping(self) -> dict[str, Any]:
        response = self.chat(
            [
                {"role": "system", "content": "Reply with exactly: ok"},
                {"role": "user", "content": "health check"},
            ],
            temperature=0,
            timeout=30,
        )
        return {"ok": bool(response.content.strip()), "content": response.content.strip()}
