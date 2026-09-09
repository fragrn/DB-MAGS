import copy
import io
import json
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.tools import LLMTimeoutError, llm_generate


FORMAT = {"type": "json_schema", "json_schema": {
    "name": "intent_v1", "strict": True,
    "schema": {"type": "object", "properties": {"intents": {"type": "array", "items": {"type": "string"}}},
               "required": ["intents"], "additionalProperties": False},
}}


def config(model="gpt-4o"):
    return RuntimeConfig(openai_api_key="test-private-api-token", mysql_password="test-private-db-password",
                         openai_model=model, openai_base_url="https://example.invalid/v1")


def completion(content='{"intents": []}', **kwargs):
    return {"id": "chatcmpl-example", "choices": [
        {"message": {"content": content}, "finish_reason": "stop"}], **kwargs}


def invoke(data, headers=None, **kwargs):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(data).encode()
    response.headers = headers or {}
    with patch("urllib.request.urlopen", return_value=response) as http:
        result = llm_generate(config(kwargs.pop("model", "gpt-4o")), "system", "user", **kwargs)
    request = http.call_args.args[0]
    return result, json.loads(request.data), request


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-5-test"])
def test_explicit_format_reaches_http_and_metadata_preserved(model):
    fmt = copy.deepcopy(FORMAT)
    result, body, request = invoke(completion(usage={"prompt_tokens": 12, "completion_tokens": 7}),
                                   headers={"x-request-id": "req-one", "Authorization": "not-returned"},
                                   response_format=fmt, json_mode=False, model=model)
    assert body["response_format"] == FORMAT == fmt
    assert body["response_format"]["json_schema"]["strict"] is True
    assert request.get_header("Authorization") == "Bearer test-private-api-token"
    assert result["parsed_payload"] == result["json_payload"] == {"intents": []}
    assert result["finish_reason"] == "stop" and result["refusal"] is None
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 7}
    assert result["usage_status"] == "available"
    assert result["request_id"] == "req-one" and result["response_id"] == "chatcmpl-example"
    assert "Authorization" not in json.dumps(result)


@pytest.mark.parametrize("model,json_mode,expected", [
    ("gpt-4o", True, {"type": "json_object"}), ("gpt-5-test", True, None),
    ("gpt-4o", False, None),
])
def test_legacy_request_policy_preserved(model, json_mode, expected):
    result, body, _ = invoke(completion(), model=model, json_mode=json_mode)
    assert body.get("response_format") == expected
    assert ("json_payload" in result) == json_mode
    assert result["usage"] is None and result["usage_status"] == "unavailable"
    assert result["request_id"] is None


def test_explicit_text_overrides_json_mode():
    result, body, _ = invoke(completion("ordinary text"), response_format={"type": "text"})
    assert body["response_format"] == {"type": "text"}
    assert result["text"] == "ordinary text" and "error" not in result
    assert "json_payload" not in result


@pytest.mark.parametrize("data", [[], None, {}, {"choices": []}, {"choices": {}},
                                   {"choices": ["wrong"]}, {"choices": [{"message": []}]},
                                   completion(None), completion(["text"])] )
def test_malformed_envelopes_never_raise_attribute_error(data):
    result, _, _ = invoke(data, response_format=FORMAT)
    assert result["error_type"] == "invalid_response"
    assert "has no attribute" not in result["error"]
    assert "json_payload" not in result


@pytest.mark.parametrize("text", ['[]', '"text"', 'null', '3', 'true'])
def test_arrays_and_scalars_are_diagnostic_only(text):
    result, _, _ = invoke(completion(text), response_format=FORMAT)
    assert result["error_type"] == "invalid_output_shape"
    assert result["parsed_payload"] == json.loads(text)
    assert "json_payload" not in result
    assert result["text"] == text


def test_legacy_array_is_also_guarded():
    result, _, _ = invoke(completion("[]"))
    assert result["error_type"] == "invalid_output_shape"


@pytest.mark.parametrize("text", ['{"intents":', 'explanation {"intents": []}', '{"value": NaN}'])
def test_strict_invalid_json_and_legacy_parse_feedback(text):
    result, _, _ = invoke(completion(text), response_format=FORMAT)
    assert result["error_type"] == "invalid_json" and result["text"] == text
    assert "parse_error" in result
    legacy, _, _ = invoke(completion(text))
    assert "error" not in legacy and "json_payload" not in legacy
    assert "parse_error" in legacy


def test_refusal_and_truncation_do_not_publish_executable_payload():
    refused = completion(None)
    refused["choices"][0]["message"]["refusal"] = "Cannot comply"
    result, _, _ = invoke(refused, response_format=FORMAT)
    assert result["error_type"] == "refusal" and result["refusal"] == "Cannot comply"
    for reason, category in [("length", "truncated_response"), ("content_filter", "content_filter")]:
        data = completion()
        data["choices"][0]["finish_reason"] = reason
        result, _, _ = invoke(data, response_format=FORMAT)
        assert result["error_type"] == category
        assert result["finish_reason"] == reason and result["text"] == '{"intents": []}'
        assert "json_payload" not in result


@pytest.mark.parametrize("usage", [None, {}, [], "unknown"])
def test_absent_usage_not_fabricated(usage):
    result, _, _ = invoke(completion(usage=usage))
    assert result["usage"] is None and result["usage_status"] == "unavailable"


def test_zero_usage_is_actual_usage_and_body_request_id_preserved():
    result, _, _ = invoke(completion(usage={"total_tokens": 0}, request_id="server-id"))
    assert result["usage"] == {"total_tokens": 0} and result["usage_status"] == "available"
    assert result["request_id"] == "server-id"


def test_unsupported_format_http_error_no_silent_retry_or_credentials():
    headers = Message()
    headers["x-request-id"] = "req-rejected"
    error = HTTPError("https://user:pass@example.invalid/v1", 400, "bad", headers,
                      io.BytesIO(b'{"error":{"message":"unsupported response_format; Bearer test-private-api-token"}}'))
    with patch("urllib.request.urlopen", side_effect=error) as http:
        result = llm_generate(config(), "system", "user", response_format=FORMAT)
    http.assert_called_once()
    assert result["http_status"] == 400 and result["error_type"] == "http_error"
    assert result["request_id"] == "req-rejected"
    assert "unsupported response_format" in result["error"]
    assert "test-private-api-token" not in json.dumps(result)
    assert "user:pass" not in json.dumps(result)


def test_timeout_preserves_exception_contract_and_redacts():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("test-private-api-token")) as http, \
         patch("AnomalyGraphAgent.tools.time.sleep"):
        with pytest.raises(LLMTimeoutError, match="timed out after 120s") as caught:
            llm_generate(config(), "system", "user", response_format=FORMAT)
    assert http.call_count == 3
    assert "test-private-api-token" not in str(caught.value)


def test_server_echoed_credentials_are_redacted_in_text_and_parsed_payload():
    data = completion('{"message": "test-private-api-token test-private-db-password"}')
    result, _, _ = invoke(data, response_format=FORMAT)
    encoded = json.dumps(result)
    assert "test-private-api-token" not in encoded and "test-private-db-password" not in encoded
    assert result["json_payload"] == json.loads(result["text"])


def test_http_200_api_error_is_structured():
    result, _, _ = invoke({"error": {"message": "unsupported model", "password": "secret"}})
    assert result["error_type"] == "api_error"
    assert "secret" not in json.dumps(result)


def test_unrecognized_credentials_echoed_in_json_are_redacted():
    result, _, _ = invoke(completion('{"Authorization":"Basic unexpected-token","password":"other-secret"}'))
    assert "unexpected-token" not in json.dumps(result)
    assert "other-secret" not in json.dumps(result)
    assert json.loads(result["text"]) == result["json_payload"]


def test_transport_failure_and_basic_auth_are_redacted():
    with patch("urllib.request.urlopen", side_effect=ConnectionError("Authorization: Basic encoded-secret")), \
         patch("AnomalyGraphAgent.tools.time.sleep"):
        result = llm_generate(config(), "system", "user", response_format=FORMAT)
    assert result["error_type"] == "transport_error"
    assert "encoded-secret" not in json.dumps(result)


@pytest.mark.parametrize("fmt", [[], "json", {}, {"type": "json_schema", "json_schema": []}])
def test_bad_format_does_not_make_request(fmt):
    with patch("urllib.request.urlopen") as http:
        result = llm_generate(config(), "system", "user", response_format=fmt)
    http.assert_not_called()
    assert result["error_type"] == "invalid_response_format"
