"""M11 tests for isolated Reflection and bounded Action regeneration."""

from __future__ import annotations

from pathlib import Path

import pytest

from db_repro_agent.agent.action_generator import ActionGenerator
from db_repro_agent.agent.chain_agent import ChainAgent
from db_repro_agent.agent.chain_reflector import ChainReflector
from db_repro_agent.agent.context_builder import ContextBuilder
from db_repro_agent.agent.contracts import ChainReproductionRequest
from db_repro_agent.agent.post_agent import PostAgent
from db_repro_agent.models.action import SQLAction
from db_repro_agent.models.reflection import ReflectionContext
from db_repro_agent.runtime.action_validator import ValidationDecision, ValidationResult
from db_repro_agent.runtime.artifact_store import ArtifactStore
from db_repro_agent.runtime.safety import SafetyChecker


def _reflection_context(**kwargs) -> ReflectionContext:
    values = {
        "source": "chain",
        "target": "slow_query",
        "previous_actions": [{"action_id": "sql-1", "duration_sec": 2}],
        "action_results": [{"action_id": "sql-1", "success": True}],
        "metrics_summary": {},
        "node_results": {},
        "edge_results": [],
        "remaining_budget": 15,
    }
    values.update(kwargs)
    return ReflectionContext(**values)


def test_chain_reflector_distinguishes_execution_failure_from_evidence_miss() -> None:
    reflector = ChainReflector()

    execution_failure = reflector.reflect(
        _reflection_context(action_results=[{"action_id": "sql-1", "success": False, "error": "timeout"}])
    )
    evidence_miss = reflector.reflect(
        _reflection_context(failed_node="slow_query", metrics_summary={"query_duration_sec": 2, "slow_query_threshold_sec": 10})
    )

    assert execution_failure.diagnosis.startswith("ACTION_EXECUTION_FAILED")
    assert evidence_miss.diagnosis.startswith("SQL_DURATION_TOO_SHORT")
    assert execution_failure.diagnosis != evidence_miss.diagnosis


@pytest.mark.parametrize(
    ("metrics", "failed_node", "expected_key"),
    [
        ({"terminals": 2, "required_terminals": 4}, "connections_up", "terminals"),
        ({"holder_lock_seconds": 2, "lock_wait_timeout_seconds": 5}, "row_lock", "holder_lock_seconds"),
        ({"query_duration_sec": 2, "slow_query_threshold_sec": 10}, "slow_query", "duration_sec"),
        ({"qps_ratio": 0.95, "baseline_qps": 100, "injection_qps": 95, "terminals": 2, "rate": 4}, "qps_drop", "terminals"),
    ],
)
def test_chain_reflector_gives_targeted_changes(metrics, failed_node, expected_key) -> None:
    result = ChainReflector().reflect(_reflection_context(metrics_summary=metrics, failed_node=failed_node))

    assert result.retryable is True
    assert expected_key in result.recommended_changes
    assert len(result.recommended_changes) <= 3
    assert "double_everything" not in result.recommended_changes


def test_chain_reflector_clamps_action_to_injection_window() -> None:
    result = ChainReflector().reflect(
        _reflection_context(
            failed_node="qps_drop",
            metrics_summary={"action_duration_sec": 30, "injection_window_seconds": 10},
        )
    )

    assert result.diagnosis.startswith("ACTION_WINDOW_MISMATCH")
    assert result.recommended_changes == {"duration_sec": 10}


def test_post_reflector_isolated_from_chain_context() -> None:
    post_agent = PostAgent()
    result = post_agent.reflect(
        ReflectionContext(
            source="post",
            target="slow_query",
            environment={"missing_information": ["row_count"]},
        )
    )

    assert result.source == "post"
    assert "request_missing_information" in result.recommended_changes
    with pytest.raises(ValueError, match="source='post'"):
        post_agent.reflect(_reflection_context())


class SequenceLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.contexts = []

    def generate_action(self, context):
        self.contexts.append(context)
        return self.payloads.pop(0)


class CountingValidator:
    def __init__(self):
        self.calls = 0

    def validate(self, action):
        self.calls += 1
        return ValidationResult(decision=ValidationDecision.PASS, action_id=action.action_id)


class CountingSafety:
    def __init__(self):
        self.calls = 0

    def check(self, action):
        self.calls += 1
        return ValidationResult(decision=ValidationDecision.PASS, action_id=action.action_id)


def _request() -> ChainReproductionRequest:
    return ChainReproductionRequest(
        incident_id="m11-chain",
        target_node="slow_query",
        propagation_chain=["traffic_surge", "slow_query"],
        database="tpcc10_test",
        duration_budget_seconds=15,
        workload_status={"benchmark": "tpcc", "alive": True},
    )


def _sql_payload(action_id: str, duration: int) -> dict:
    return {
        "type": "sql",
        "action_id": action_id,
        "target_node": "slow_query",
        "database": "tpcc10_test",
        "sql": "SELECT SLEEP(2)",
        "duration_sec": duration,
    }


def test_chain_agent_regenerates_full_plan_and_revalidates_each_attempt(tmp_path: Path) -> None:
    llm = SequenceLLM([_sql_payload("attempt-1", 2), _sql_payload("attempt-2", 10)])
    generator = ActionGenerator(llm)
    agent = ChainAgent(generator)
    validator = CountingValidator()
    safety = CountingSafety()
    calls = []

    def runner(plan, attempt):
        calls.append((plan.action.action_id, attempt))
        if attempt == 1:
            return {
                "success": False,
                "action_result": {"action_id": plan.action.action_id, "success": True},
                "metrics_summary": {"query_duration_sec": 2, "slow_query_threshold_sec": 10},
                "failed_node": "slow_query",
            }
        return {"success": True, "action_result": {"action_id": plan.action.action_id, "success": True}}

    result = agent.reproduce_chain_with_reflection(
        _request(),
        attempt_runner=runner,
        validator=validator,
        safety_checker=safety,
        max_retry_rounds=1,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        experiment="m11-chain",
    )

    assert result.success is True
    assert calls == [("attempt-1", 1), ("attempt-2", 2)]
    assert validator.calls == 2
    assert safety.calls == 2
    assert result.attempts[0].reflection is not None
    assert result.attempts[0].reflection.recommended_changes["duration_sec"] == 10
    assert llm.contexts[1]["previous_reflection"]["source"] == "chain"
    assert (tmp_path / "artifacts" / "m11-chain" / "chain" / "attempt_001" / "reflection.json").exists()
    assert (tmp_path / "artifacts" / "m11-chain" / "chain" / "attempt_002" / "action_plan.json").exists()


def test_chain_agent_stops_after_retry_budget() -> None:
    llm = SequenceLLM([_sql_payload("attempt-1", 2), _sql_payload("attempt-2", 2)])
    agent = ChainAgent(ActionGenerator(llm))

    result = agent.reproduce_chain_with_reflection(
        _request(),
        attempt_runner=lambda plan, attempt: {
            "success": False,
            "action_result": {"action_id": plan.action.action_id, "success": True},
            "failed_node": "slow_query",
            "metrics_summary": {"query_duration_sec": 2, "slow_query_threshold_sec": 10},
        },
        validator=CountingValidator(),
        safety_checker=CountingSafety(),
        max_retry_rounds=1,
    )

    assert result.success is False
    assert len(result.attempts) == 2
    assert result.message == "Chain reproduction exhausted Reflection retries"
