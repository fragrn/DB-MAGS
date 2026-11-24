"""Real LLM API integration test for M10 ActionPlan generation.

This test never executes the generated database Action. It only verifies the
configuration, SDK call, Responses-first/fallback client behavior, and strict
ActionPlan parsing.
"""

from __future__ import annotations

import os

import pytest

from db_repro_agent.agent.action_generator import ActionGenerator
from db_repro_agent.agent.context_builder import ContextBuilder
from db_repro_agent.config import load_config
from db_repro_agent.models.action import (
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
)
from db_repro_agent.runtime.safety import SafetyChecker


pytestmark = pytest.mark.integration


def _llm_context():
    return ContextBuilder().build(
        target_anomaly="slow_query",
        target_node="slow_query",
        database="tpcc10_test",
        schema={
            "warehouse": {
                "columns": ["w_id", "w_ytd", "w_tax", "w_name"],
            }
        },
        indexes={"warehouse": ["PRIMARY(w_id)"]},
        data_scale={"warehouse": 1000},
        runtime_snapshot={"threads_running": 1, "threads_connected": 2},
        workload_status={"benchmark": "tpcc", "alive": True},
        duration_budget_seconds=15,
        safety_constraints={
            "max_duration_seconds": 15,
            "max_concurrency": 8,
            "forbidden_operations": ["DROP DATABASE", "TRUNCATE"],
        },
    )


@pytest.fixture(scope="module")
def real_generator() -> ActionGenerator:
    if os.environ.get("DBMAGS_RUN_LLM_API_TEST") != "1":
        pytest.skip("set DBMAGS_RUN_LLM_API_TEST=1 to call the real LLM API")

    config = load_config()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", config.llm.api_key),
            ("OPENAI_BASE_URL", config.llm.base_url),
            ("OPENAI_MODEL", config.llm.model),
        )
        if not value
    ]
    if missing:
        pytest.fail(f"LLM configuration is missing: {', '.join(missing)}")
    return ActionGenerator.from_config(config.llm)


def _generate(real_generator: ActionGenerator, context):
    try:
        plan = real_generator.generate(context)
    except Exception as exc:
        config = load_config()
        safe_message = str(exc).replace(config.llm.api_key, "<redacted>")
        pytest.fail(
            f"LLM API/ActionPlan failed for target={context.target_node}: "
            f"error_type={type(exc).__name__}, error={safe_message}"
        )
    print(f"LLM generated target={context.target_node} type={plan.action.type} action_id={plan.action.action_id}")
    return plan


def _context_for(target_node: str, **constraints):
    value = _llm_context()
    return value.model_copy(
        update={
            "target_anomaly": target_node,
            "target_node": target_node,
            "safety_constraints": {
                **value.safety_constraints,
                **constraints,
            },
        }
    )


def test_real_llm_api_generates_sql_action_plan(real_generator: ActionGenerator) -> None:
    plan = _generate(real_generator, _context_for("slow_query"))

    assert isinstance(plan.action, SQLAction)
    assert plan.action.action_id
    assert plan.action.target_node == "slow_query"
    assert plan.action.database == "tpcc10_test"
    assert plan.action.sql.strip()
    assert "query" not in plan.action.model_dump()
    assert plan.action.duration_sec <= 15
    assert plan.action.concurrency <= 8


def test_real_llm_api_generates_transaction_action_plan(real_generator: ActionGenerator) -> None:
    context = _context_for(
        "row_lock",
        lock_strategy="holder_waiter",
        holder_lock_seconds=10,
        lock_wait_timeout_seconds=30,
    )
    plan = _generate(real_generator, context)

    assert isinstance(plan.action, TransactionAction)
    assert plan.action.action_id
    assert plan.action.target_node == "row_lock"
    assert plan.action.database == "tpcc10_test"
    assert {actor.role for actor in plan.action.actors} >= {"holder", "waiter"}
    assert all(actor.actor_id and actor.steps for actor in plan.action.actors)
    assert all(step.sql.strip() for actor in plan.action.actors for step in actor.steps)
    SafetyChecker(target_database="tpcc10_test").check(plan.action)


def test_real_llm_api_generates_benchbase_action_plan(real_generator: ActionGenerator) -> None:
    context = _context_for(
        "traffic_surge",
        benchmark="tpcc",
        allowed_benchmarks=["tpcc", "tpch"],
        terminals_max=8,
        duration_max_seconds=180,
        benchbase_jar_path=".tools/benchbase-main/target/benchbase-mysql/benchbase.jar",
        benchbase_config_path=".tools/benchbase-main/target/benchbase-mysql/config/mysql/sample_tpcc_config.xml",
        results_dir="experiment_runs/llmapi",
    )
    plan = _generate(real_generator, context)

    assert isinstance(plan.action, BenchBaseAction)
    assert plan.action.action_id
    assert plan.action.target_node == "traffic_surge"
    assert plan.action.benchmark.lower() in {"tpcc", "tpch"}
    assert 1 <= plan.action.terminals <= 8
    assert 1 <= plan.action.duration_sec <= 180
    assert plan.action.config_path
    assert plan.action.jar_path
    assert plan.action.results_dir


def test_real_llm_api_generates_chaosblade_action_plan(real_generator: ActionGenerator) -> None:
    context = _context_for(
        "resource_cpu",
        resource="cpu",
        allowed_resources=["cpu"],
        cpu_percent_min=1,
        cpu_percent_max=80,
        chaosblade_path=".tools/chaosblade-1.8.0-darwin_arm64/blade",
    )
    plan = _generate(real_generator, context)

    assert isinstance(plan.action, ChaosBladeAction)
    assert plan.action.action_id
    assert plan.action.target_node == "resource_cpu"
    assert plan.action.resource == "cpu"
    assert plan.action.command.strip()
    assert plan.action.blade_path
    assert 1 <= plan.action.duration_sec <= 15
    safety_result = SafetyChecker(target_database="tpcc10_test").check(plan.action)
    assert safety_result.passed, safety_result.issues
