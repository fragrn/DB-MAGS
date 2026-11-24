"""M10 tests for LLM-generated final Actions and quality statistics."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from db_repro_agent.agent.action_generator import (
    ActionGenerationError,
    ActionGenerator,
    evaluate_batch,
)
from db_repro_agent.agent.chain_agent import ChainAgent
from db_repro_agent.agent.context_builder import ContextBuilder
from db_repro_agent.agent.contracts import ChainReproductionRequest, PostReproductionRequest
from db_repro_agent.agent.llm_client import LLMClientError, OpenAICompatibleClient
from db_repro_agent.agent.post_agent import PostAgent
from db_repro_agent.config import LLMConfig, load_config
from db_repro_agent.models.action import BenchBaseAction, SQLAction, TransactionAction
from db_repro_agent.runtime.action_validator import ActionValidator
from db_repro_agent.runtime.safety import SafetyChecker


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.contexts = []

    def generate_action(self, context):
        self.contexts.append(context)
        return self.payload


def context(target_node: str, **kwargs):
    return ContextBuilder().build(
        target_anomaly=target_node,
        target_node=target_node,
        database="tpcc10_test",
        schema={"warehouse": {"columns": ["w_id", "w_ytd"]}},
        indexes={"warehouse": ["PRIMARY(w_id)"]},
        data_scale={"warehouse": 1000},
        runtime_snapshot={"threads_running": 2},
        workload_status={"benchmark": "tpcc", "alive": True},
        duration_budget_seconds=15,
        safety_constraints={"max_concurrency": 8},
        previous_reflection=kwargs.get("previous_reflection"),
    )


def test_context_builder_preserves_generation_context() -> None:
    value = context("hot_update", previous_reflection={"reason": "previous action was too large"})

    assert value.target_node == "hot_update"
    assert value.schema_["warehouse"]["columns"] == ["w_id", "w_ytd"]
    assert value.previous_reflection == {"reason": "previous action was too large"}
    assert value.model_dump(mode="json")["duration_budget_seconds"] == 15.0


def test_hot_update_generates_final_sql_without_intent_layer() -> None:
    fake = FakeLLM(
        {
            "action": {
                "type": "sql",
                "action_id": "hot-update-1",
                "target_node": "hot_update",
                "database": "tpcc10_test",
                "sql": "UPDATE warehouse SET w_ytd = LEAST(w_ytd + 0.01, 2147483647) WHERE w_id = 1",
                "concurrency": 3,
                "duration_sec": 10,
                "timeout_sec": 30,
                "execution_mode": "concurrent",
            }
        }
    )
    plan = ActionGenerator(fake).generate(context("hot_update"))

    assert isinstance(plan.action, SQLAction)
    assert plan.action.sql.startswith("UPDATE warehouse")
    assert "LEAST" in plan.action.sql
    assert fake.contexts[0]["target_node"] == "hot_update"


def test_lock_contention_generates_holder_and_waiter() -> None:
    plan = ActionGenerator.parse(
        {
            "action": {
                "type": "transaction",
                "action_id": "lock-1",
                "target_node": "row_lock",
                "database": "tpcc10_test",
                "actors": [
                    {"actor_id": "holder", "role": "holder", "steps": [{"sql": "BEGIN"}, {"sql": "SELECT w_id FROM warehouse WHERE w_id=1 FOR UPDATE", "delay_after_sec": 10}]},
                    {"actor_id": "waiter", "role": "waiter", "steps": [{"sql": "BEGIN"}, {"sql": "UPDATE warehouse SET w_ytd=w_ytd+0.01 WHERE w_id=1"}, {"sql": "COMMIT"}]},
                ],
                "duration_sec": 15,
                "timeout_sec": 30,
            }
        }
    )

    assert isinstance(plan.action, TransactionAction)
    assert {actor.role for actor in plan.action.actors} == {"holder", "waiter"}


@pytest.mark.parametrize(
    "payload, expected_type",
    [
        (
            {"type": "sql", "action_id": "slow-1", "target_node": "slow_query", "database": "tpcc10_test", "sql": "SELECT SLEEP(2)", "duration_sec": 3, "timeout_sec": 10},
            SQLAction,
        ),
        (
            {"type": "benchbase", "action_id": "traffic-1", "target_node": "traffic_surge", "benchmark": "tpcc", "database": "tpcc10_test", "terminals": 4, "rate": 10, "duration_sec": 60, "config_path": "config.xml", "jar_path": "benchbase.jar", "results_dir": "results"},
            BenchBaseAction,
        ),
    ],
)
def test_slow_sql_and_traffic_actions_are_structured(payload, expected_type) -> None:
    plan = ActionGenerator.parse(payload)

    assert isinstance(plan.action, expected_type)
    assert plan.action.action_id


def test_invalid_structured_output_is_rejected() -> None:
    with pytest.raises(ActionGenerationError):
        ActionGenerator.parse({"action": {"type": "sql", "action_id": "bad"}})

    with pytest.raises(ActionGenerationError):
        ActionGenerator.parse({"action": {"type": "sql", "action_id": "bad", "target_node": "slow_query", "database": "tpcc10_test", "sql": "SELECT 1", "unexpected": True}})

    with pytest.raises(ActionGenerationError):
        ActionGenerator.parse(
            {
                "type": "benchbase",
                "action_id": "invalid-rate",
                "target_node": "traffic_surge",
                "benchmark": "tpcc",
                "database": "tpcc10_test",
                "terminals": 4,
                "rate": 0,
                "duration_sec": 60,
                "config_path": "config.xml",
                "jar_path": "benchbase.jar",
                "results_dir": "results",
            }
        )


def test_benchbase_rate_may_be_omitted_or_positive() -> None:
    base = {
        "type": "benchbase",
        "action_id": "traffic-rate",
        "target_node": "traffic_surge",
        "benchmark": "tpcc",
        "database": "tpcc10_test",
        "terminals": 4,
        "duration_sec": 60,
        "config_path": "config.xml",
        "jar_path": "benchbase.jar",
        "results_dir": "results",
    }

    omitted = ActionGenerator.parse(base)
    positive = ActionGenerator.parse({**base, "rate": 1})

    assert isinstance(omitted.action, BenchBaseAction)
    assert omitted.action.rate is None
    assert isinstance(positive.action, BenchBaseAction)
    assert positive.action.rate == 1


def test_generated_action_uses_existing_validator(tmp_path: Path) -> None:
    class SchemaProvider:
        def probe_schema(self, database):
            return {"tables": [{"table_name": "warehouse", "columns": [{"name": "w_id"}, {"name": "w_ytd"}]}]}

    plan = ActionGenerator.parse(
        {"type": "sql", "action_id": "valid-1", "target_node": "hot_update", "database": "tpcc10_test", "sql": "UPDATE warehouse SET w_ytd=w_ytd+0.01 WHERE w_id=1"}
    )
    validator = ActionValidator(target_database="tpcc10_test", schema_provider=SchemaProvider(), allowed_path_roots=[tmp_path])

    result = ActionGenerator.validate(plan, validator)

    assert result.passed is True


def test_batch_statistics_distinguish_all_m10_stages() -> None:
    valid = [
        {"type": "sql", "action_id": f"a-{index}", "target_node": "slow_query", "database": "tpcc10_test", "sql": "SELECT SLEEP(1)"}
        for index in range(80)
    ]
    raw_outputs = valid + [{"action": {"type": "sql", "action_id": f"bad-{index}"}} for index in range(20)]

    stats = evaluate_batch(
        raw_outputs,
        validator=lambda action: int(action.action_id.split("-")[-1]) < 70,
        executor=lambda action: int(action.action_id.split("-")[-1]) < 60,
        oracle=lambda action: int(action.action_id.split("-")[-1]) < 30,
    )

    assert stats.total == 100
    assert stats.structured_output_success == 80
    assert stats.validation_pass == 70
    assert stats.execution_success == 60
    assert stats.anomaly_hit == 30
    assert stats.structured_output_success_rate == 0.8
    assert stats.validation_pass_rate == 0.7
    assert stats.execution_success_rate == 0.6
    assert stats.anomaly_hit_rate == 0.3


def _sdk_response(text: str):
    return SimpleNamespace(output_text=text)


def test_sdk_defaults_to_responses_and_returns_untrusted_json() -> None:
    sdk = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _sdk_response('{"action": {"type": "sql"}}')
        ),
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: None)),
    )
    with patch("db_repro_agent.agent.llm_client.OpenAI", return_value=sdk):
        client = OpenAICompatibleClient(
            LLMConfig(base_url="https://relay.example/v1", api_key="key", model="gpt-5.6-terra")
        )
    result = client.generate_action({"target_node": "slow_query"})

    assert result["action"]["type"] == "sql"


def test_sdk_falls_back_to_chat_only_for_responses_incompatibility() -> None:
    calls = []

    def responses_create(**kwargs):
        calls.append("responses")
        raise RuntimeError("responses endpoint unsupported by gateway")

    def chat_create(**kwargs):
        calls.append("chat")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"type": "sql"}'))]
        )

    sdk = SimpleNamespace(
        responses=SimpleNamespace(create=responses_create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
    )
    with patch("db_repro_agent.agent.llm_client.OpenAI", return_value=sdk):
        client = OpenAICompatibleClient(
            LLMConfig(base_url="https://relay.example/v1", api_key="key", model="gpt-5.6-terra")
        )
    client._is_responses_unsupported = lambda exc: True

    result = client.generate_action({"target_node": "slow_query"})

    assert calls == ["responses", "chat"]
    assert result == {"type": "sql"}


def test_non_compatibility_responses_error_does_not_fallback() -> None:
    sdk = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("timeout"))),
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: pytest.fail("must not call chat"))),
    )
    with patch("db_repro_agent.agent.llm_client.OpenAI", return_value=sdk):
        client = OpenAICompatibleClient(
            LLMConfig(base_url="https://relay.example/v1", api_key="key", model="gpt-5.6-terra")
        )
    with pytest.raises(LLMClientError, match="Responses request failed"):
        client.generate_action({"target_node": "slow_query"})


def test_safety_gate_rejects_before_executor() -> None:
    plan = ActionGenerator.parse(
        {
            "type": "sql",
            "action_id": "unsafe-1",
            "target_node": "slow_query",
            "database": "tpcc10_test",
            "sql": "DROP DATABASE mysql",
        }
    )
    validator = ActionValidator(target_database="tpcc10_test")
    safety = SafetyChecker(target_database="tpcc10_test")

    result = ActionGenerator.validate_for_execution(
        plan, validator=validator, safety_checker=safety
    )

    assert result.passed is False
    assert any(issue.code == "DROP_DATABASE_FORBIDDEN" for issue in result.issues)


def test_generate_validated_is_the_executor_entry_gate() -> None:
    fake = FakeLLM(
        {
            "type": "sql",
            "action_id": "safe-1",
            "target_node": "slow_query",
            "database": "tpcc10_test",
            "sql": "SELECT SLEEP(1)",
        }
    )
    generator = ActionGenerator(fake)
    validator = ActionValidator(target_database="tpcc10_test")
    safety = SafetyChecker(target_database="tpcc10_test")

    plan = generator.generate_validated(
        context("slow_query"), validator=validator, safety_checker=safety
    )

    assert isinstance(plan.action, SQLAction)


def test_api_mode_can_be_selected_from_environment() -> None:
    config = load_config(
        environ={
            "OPENAI_BASE_URL": "https://relay.example/v1",
            "OPENAI_API_KEY": "key",
            "OPENAI_MODEL": "gpt-5.6-terra",
            "OPENAI_API_MODE": "chat_completions",
        }
    )

    assert config.llm.api_mode == "chat_completions"


def test_llm_timeout_defaults_are_bounded_and_logged(capsys) -> None:
    config = LLMConfig(
        base_url="https://relay.example/v1",
        api_key="key",
        model="gpt-5.6-terra",
    )
    sdk = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: None),
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: None)),
    )
    with patch("db_repro_agent.agent.llm_client.OpenAI", return_value=sdk):
        client = OpenAICompatibleClient(config)

    assert client.timeout_seconds == 45.0
    assert client.max_retries == 1
    client._log_no_response("responses", TimeoutError("no response"))

    assert "LLM API received no response" in capsys.readouterr().out


def chain_request() -> ChainReproductionRequest:
    return ChainReproductionRequest(
        incident_id="chain-m10",
        target_node="slow_query",
        propagation_chain=["traffic_surge", "row_lock", "slow_query"],
        database="tpcc10_test",
        schema={"warehouse": {"columns": ["w_id", "w_ytd"]}},
        indexes={"warehouse": ["PRIMARY(w_id)"]},
        data_scale={"warehouse": 1000},
        runtime_snapshot={"threads_running": 2},
        workload_status={"benchmark": "tpcc", "alive": True},
        active_injection_nodes=["traffic_surge"],
        observed_downstream_nodes=["row_lock", "slow_query"],
        duration_budget_seconds=15,
        safety_constraints={"max_concurrency": 8},
    )


def test_chain_agent_passes_chain_and_environment_into_generation_context() -> None:
    fake = FakeLLM(
        {
            "action": {
                "type": "sql",
                "action_id": "chain-action-1",
                "target_node": "slow_query",
                "database": "tpcc10_test",
                "sql": "SELECT SLEEP(2)",
            }
        }
    )
    agent = ChainAgent(ActionGenerator(fake))

    plan = agent.generate_action_plan(chain_request())

    assert isinstance(plan.action, SQLAction)
    generated_context = fake.contexts[0]
    assert generated_context["propagation_chain"] == [
        "traffic_surge",
        "row_lock",
        "slow_query",
    ]
    assert generated_context["active_injection_nodes"] == ["traffic_surge"]
    assert generated_context["observed_downstream_nodes"] == ["row_lock", "slow_query"]
    assert generated_context["schema"]["warehouse"]["columns"] == ["w_id", "w_ytd"]


def test_chain_agent_validates_before_execution_and_returns_m10_result() -> None:
    fake = FakeLLM(
        {
            "type": "sql",
            "action_id": "chain-action-2",
            "target_node": "slow_query",
            "database": "tpcc10_test",
            "sql": "SELECT SLEEP(2)",
        }
    )
    agent = ChainAgent(ActionGenerator(fake))
    result = agent.reproduce_chain(
        chain_request(),
        validator=ActionValidator(target_database="tpcc10_test"),
        safety_checker=SafetyChecker(target_database="tpcc10_test"),
    )

    assert result.agent == "chain"
    assert result.status == "action_generated"
    assert result.validation is not None and result.validation.passed
    assert result.action_plan is not None


def test_post_agent_is_only_a_stable_m10_placeholder() -> None:
    result = PostAgent().reproduce_post(
        PostReproductionRequest(incident_id="post-m10", post="A DBA incident post")
    )

    assert result.agent == "post"
    assert result.status == "not_implemented"
    assert result.action_plan is None
