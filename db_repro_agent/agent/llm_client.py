"""SDK-backed LLM clients for structured ActionPlan generation."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from ..config import LLMConfig


logger = logging.getLogger("db_repro_agent.agent.llm_client")


class LLMClient(Protocol):
    def generate_action(self, context: dict[str, Any]) -> Any:
        """Return untrusted JSON representing an ActionPlan or Action."""


class LLMClientError(RuntimeError):
    """Raised when the configured LLM cannot return structured JSON."""


class OpenAICompatibleClient:
    """Use the OpenAI Python SDK with Responses-first fallback behavior.

    Responses is the default. Chat Completions is attempted only when the
    gateway explicitly appears not to support the Responses endpoint.
    """

    _SYSTEM_PROMPT = """You are the Action Generation component of a DBA incident reproduction agent.

Generate exactly one final ActionPlan for the requested target anomaly or propagation
node. The database environment and propagation context in the user message are
authoritative. Do not use ReAct, ask for more database access, or invent objects that
are not present in the context.

Return JSON only. The root object must contain `action` and may contain `rationale`.
The action must use exactly one of these discriminated types:

1. SQLAction:
   type="sql", action_id, target_node, database, sql;
   optional concurrency, duration_sec, timeout_sec, execution_mode where execution_mode
   is "single" or "concurrent".
2. TransactionAction:
   type="transaction", action_id, target_node, database, actors, optional duration_sec
   and timeout_sec. Every actor has actor_id, role (holder/waiter/worker), and steps;
   every step has sql and optional delay_after_sec.
3. BenchBaseAction:
   type="benchbase", action_id, target_node, benchmark, database, terminals,
   duration_sec, config_path, jar_path, results_dir; optional rate,
   transaction_weights, java_bin.
   `rate` is optional: omit it when no rate limit is needed; if it is present,
   it must be an integer greater than or equal to 1. Never emit `rate: 0`.
4. ChaosBladeAction:
   type="chaosblade", action_id, target_node, resource (cpu/memory/disk/network),
   command, duration_sec, blade_path.
   The command must begin with one of these exact resource prefixes:
   - resource="cpu": "cpu load" or "cpu fullload"
   - resource="memory": "mem load"
   - resource="disk": "disk fill" or "disk load"
   - resource="network": "network delay", "network loss", or "network packetloss"
   Do not prepend "blade" or "create" to command. Do not use shell control
   characters such as ;, &&, ||, |, backticks, $(), >, or <. CPU load may use
   --cpu-percent; there is no default CPU percentage cap in this experiment.

Field names are exact: use `sql`, never `query`; always include action_id and target_node.
Do not add fields outside this schema. Keep duration and concurrency within the supplied
budget and safety constraints. Do not generate DROP DATABASE, destructive system
operations, unsafe paths, or commands outside the supplied safety policy.

Do not execute an Action, silently repair an Action, or rewrite SQL after generation.
The returned JSON will be rejected or regenerated if Pydantic, ActionValidator, or
SafetyChecker rejects it."""

    def __init__(self, config: LLMConfig) -> None:
        if not config.base_url or not config.api_key or not config.model:
            raise ValueError("LLM base_url, api_key and model are required")
        self.base_url = config.base_url.rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self.api_mode = config.api_mode
        self.timeout_seconds = config.request_timeout_seconds
        self.max_retries = config.max_retries
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            # Retries are controlled here so connection diagnostics and the
            # total retry budget are not multiplied by SDK-level retries.
            max_retries=0,
        )

    def generate_action(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.api_mode == "chat_completions":
            return self._generate_chat(context)
        try:
            return self._generate_responses(context)
        except (APITimeoutError, APIConnectionError) as exc:
            self._log_no_response("responses", exc)
            raise LLMClientError(
                f"Responses request received no response within {self.timeout_seconds:g}s"
            ) from exc
        except Exception as exc:
            if not self._is_responses_unsupported(exc):
                raise LLMClientError(f"Responses request failed: {exc}") from exc
            try:
                return self._generate_chat(context)
            except Exception as chat_exc:
                raise LLMClientError(
                    f"Responses unsupported and Chat Completions fallback failed: {chat_exc}"
                ) from chat_exc

    def _generate_responses(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._request_with_retries(
                "responses",
                lambda: self.client.responses.create(
                    model=self.model,
                    instructions=self._SYSTEM_PROMPT,
                    input=json.dumps(context, ensure_ascii=False),
                    text={"format": {"type": "json_object"}},
                ),
            )
        except (APITimeoutError, APIConnectionError):
            raise
        content = getattr(response, "output_text", None)
        if not isinstance(content, str):
            raise LLMClientError("Responses result has no output_text")
        return self._parse_json(content)

    def _generate_chat(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._request_with_retries(
                "chat/completions",
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": self._SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                    ],
                ),
            )
        except (APITimeoutError, APIConnectionError) as exc:
            self._log_no_response("chat/completions", exc)
            raise LLMClientError(
                f"Chat Completions request received no response within {self.timeout_seconds:g}s"
            ) from exc
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMClientError("Chat response has no choices[0].message.content") from exc
        if not isinstance(content, str):
            raise LLMClientError("Chat message content must be a JSON string")
        return self._parse_json(content)

    def _request_with_retries(self, endpoint: str, request):
        """Retry only transport/timeouts with one bounded application budget."""
        for attempt in range(self.max_retries + 1):
            try:
                return request()
            except (APITimeoutError, APIConnectionError) as exc:
                self._log_no_response(endpoint, exc)
                if attempt >= self.max_retries:
                    raise
                delay = min(1.0, 0.25 * (attempt + 1))
                print(
                    f"[LLM_RETRY] endpoint={endpoint} retry={attempt + 1}/{self.max_retries} "
                    f"delay_seconds={delay:g}",
                    flush=True,
                )
                time.sleep(delay)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"LLM content is not valid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise LLMClientError("LLM JSON output must be an object")
        return result

    @staticmethod
    def _is_responses_unsupported(exc: Exception) -> bool:
        if isinstance(exc, APIStatusError) and exc.status_code in {400, 404, 405, 415, 422}:
            return True
        if isinstance(exc, APIError):
            message = str(exc).lower()
            return any(
                token in message
                for token in ("responses", "unsupported", "not found", "unknown endpoint")
            )
        return False

    def _log_no_response(self, endpoint: str, exc: Exception) -> None:
        message = (
            f"LLM API received no response: endpoint={endpoint} model={self.model} "
            f"timeout_seconds={self.timeout_seconds:g} max_retries={self.max_retries} "
            f"error_type={type(exc).__name__}"
        )
        logger.error(message)
        # Keep the diagnostic visible when the caller has not configured the
        # package logger, including `pytest -s` integration runs.
        print(f"[LLM_TIMEOUT] {message}", flush=True)
