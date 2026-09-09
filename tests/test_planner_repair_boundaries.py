"""Offline repair budgets, immutable scope and infrastructure boundaries."""

import copy
from unittest.mock import patch

import pytest

from AnomalyGraphAgent.planner import GlobalPlanner, PlannerFallbackError, PlannerLLMError
from AnomalyGraphAgent.intent_compiler import CompilationContext, compile_intent_plan
from test_intent_planner_integration import setup


@pytest.fixture(autouse=True)
def no_execution():
    with patch("AnomalyGraphAgent.executor.Executor.execute") as execute:
        yield
        execute.assert_not_called()


def response(payload):
    return {"json_payload": copy.deepcopy(payload)}


def test_semantic_and_json_share_budget():
    cfg, req, snap, good = setup()
    bad = copy.deepcopy(good)
    bad["intents"][0]["sql"]["waiter_sql"] = "UPDATE district SET absent=1 WHERE d_id=1"
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=[{"text": "["}, response(bad), response(good)]) as llm, patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": []}):
        planner.plan(req, snap)
    assert llm.call_count == 3
    assert planner.planner_json_repair_attempts == 2
    assert planner.sql_repair_attempts == 0


def test_invalid_json_exhaustion():
    cfg, req, snap, _ = setup()
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"text": "["}) as llm, patch("AnomalyGraphAgent.tools.explain_sql") as explain:
        with pytest.raises(PlannerFallbackError) as caught:
            planner.plan(req, snap)
    assert llm.call_count == 3
    assert caught.value.error_type == "intent_repair_exhausted"
    assert caught.value.__cause__ is not None
    explain.assert_not_called()


def test_each_sql_has_independent_budget():
    cfg, req, snap, good = setup()
    planner = GlobalPlanner(cfg)
    seen = set()

    def explain(config, database, sql):
        if sql not in seen:
            seen.add(sql)
            return {"plan": [{"error": "SQL rejected"}]}
        return {"plan": []}

    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.tools.explain_sql", side_effect=explain):
        planner.plan(req, snap)
    assert llm.call_count == 3
    assert planner.sql_repair_counts == {"intents[0].sql.holder_sql": 1, "intents[0].sql.waiter_sql": 1}


def test_sql_exhaustion_after_three_repairs():
    cfg, req, snap, good = setup()
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"error": "bad SQL"}]}):
        with pytest.raises(PlannerFallbackError) as caught:
            planner.plan(req, snap)
    assert llm.call_count == 4
    assert planner.sql_repair_counts == {"intents[0].sql.holder_sql": 3}
    assert caught.value.error_type == "sql_repair_exhausted"


@pytest.mark.parametrize("change", ["load", "node", "strategy", "predicate", "other_sql", "extra_sql", "extra_intent"])
def test_sql_repair_cannot_change_scope(change):
    cfg, req, snap, good = setup()
    bad = copy.deepcopy(good)
    item = bad["intents"][0]
    if change == "load":
        item["load"]["concurrency"] = 9
    elif change == "node":
        item["node"] = "long_tx"
    elif change == "strategy":
        item["strategy"] = "unknown"
    elif change == "predicate":
        item["target"]["predicate"] = "d_id=2"
    elif change == "other_sql":
        item["sql"]["holder_sql"] = item["sql"]["holder_sql"].replace("SELECT *", "SELECT d_id")
    elif change == "extra_sql":
        item["sql"]["another_sql"] = "SELECT 1"
    else:
        bad["intents"].append(copy.deepcopy(item))
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=[response(good), response(bad)]) as llm, patch("AnomalyGraphAgent.tools.explain_sql", side_effect=[{"plan": []}, {"plan": [{"error": "waiter failed"}]}]):
        with pytest.raises(PlannerFallbackError) as caught:
            planner.plan(req, snap)
    assert llm.call_count == 2
    assert caught.value.error_type == "repair_scope_violation"


def test_malformed_sql_repair_consumes_both_budgets():
    cfg, req, snap, good = setup()
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=[response(good), {"text": "["}, response(good)]) as llm, patch("AnomalyGraphAgent.tools.explain_sql", side_effect=[{"plan": [{"error": "bad SQL"}]}, {"plan": []}, {"plan": []}]):
        planner.plan(req, snap)
    assert llm.call_count == 3
    assert planner.sql_repair_counts == {"intents[0].sql.holder_sql": 2}
    assert planner.planner_json_repair_attempts == 1


def test_compiler_unexpected_error_keeps_cause():
    cfg, req, snap, good = setup()
    failure = AttributeError("broken compiler")
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.planner.compile_intent_plan", side_effect=failure), patch("AnomalyGraphAgent.tools.explain_sql") as explain:
        with pytest.raises(PlannerFallbackError) as caught:
            GlobalPlanner(cfg).plan(req, snap)
    assert caught.value.error_type == "compiler_error"
    assert caught.value.stage == "compile_intent"
    assert caught.value.__cause__ is failure
    assert llm.call_count == 1
    explain.assert_not_called()


def test_fixed_compiler_sql_never_uses_repair():
    cfg, req, snap, good = setup()
    compiled = compile_intent_plan(good, CompilationContext(request=req, config=cfg, run_id="fixed"))
    compiled.task_specs[0].actions[0]["scripts"][0]["steps"].append({"sql": "SELECT 1"})
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.planner.compile_intent_plan", return_value=compiled), patch("AnomalyGraphAgent.tools.explain_sql", side_effect=lambda config, database, sql: {"plan": [{"error": "fixed failed"}]} if sql == "SELECT 1" else {"plan": []}):
        with pytest.raises(PlannerFallbackError) as caught:
            planner.plan(req, snap)
    assert caught.value.error_type == "compiler_error"
    assert planner.sql_repair_attempts == 0
    assert llm.call_count == 1


@pytest.mark.parametrize("during_repair", [False, True])
def test_api_interruption_stops_immediately(during_repair):
    cfg, req, snap, good = setup()
    failure = ConnectionError("API offline")
    replies = [response(good), failure] if during_repair else [failure]
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=replies) as llm, patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"error": "SQL failed"}]}):
        with pytest.raises(PlannerLLMError) as caught:
            GlobalPlanner(cfg).plan(req, snap)
    assert llm.call_count == len(replies)
    assert caught.value.error_type == "llm_connection_error"
    assert caught.value.__cause__ is failure


def test_probe_attribute_error_is_not_sql_repair():
    cfg, req, snap, good = setup()
    failure = AttributeError("broken probe")
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.tools.explain_sql", side_effect=failure):
        with pytest.raises(PlannerFallbackError) as caught:
            GlobalPlanner(cfg).plan(req, snap)
    assert caught.value.error_type == "internal_error"
    assert caught.value.stage == "sql_explain"
    assert caught.value.__cause__ is failure
    assert llm.call_count == 1


def test_invalid_target_reference_can_be_repaired_without_scope_change():
    from AnomalyGraphAgent.intent_repair import guard_sql_repair
    before = {"intents": [{"node": "improper_sql", "strategy": "non_sargable_filter",
        "target": {"tables": ["district"], "columns": ["absent"]},
        "load": {"concurrency": 8, "duration_sec": 15},
        "sql": {"query": "SELECT absent FROM district"}, "expected_evidence": []}]}
    after = copy.deepcopy(before)
    after["intents"][0]["target"]["columns"] = ["d_id"]
    after["intents"][0]["sql"]["query"] = "SELECT d_id FROM district"
    guard_sql_repair(before, after, "intents[0].sql.query", {"district": ["d_id"]})
    after["intents"][0]["load"]["concurrency"] = 9
    with pytest.raises(ValueError):
        guard_sql_repair(before, after, "intents[0].sql.query", {"district": ["d_id"]})


def test_connection_probe_error_does_not_trigger_sql_repair():
    cfg, req, snap, good = setup()
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value=response(good)) as llm, patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"error": "disconnected", "error_type": "database_error"}]}):
        with pytest.raises(PlannerFallbackError) as caught:
            GlobalPlanner(cfg).plan(req, snap)
    assert caught.value.error_type == "database_connection_error"
    assert llm.call_count == 1


def test_malformed_repairs_cannot_extend_sql_budget():
    cfg, req, snap, good = setup()
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=[response(good), {"text": "["}, {"text": "["}, response(good)]) as llm, patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"error": "bad SQL"}]}):
        with pytest.raises(PlannerFallbackError) as caught:
            planner.plan(req, snap)
    assert llm.call_count == 4
    assert planner.planner_json_repair_attempts == 2
    assert planner.sql_repair_counts == {"intents[0].sql.holder_sql": 3}
    assert caught.value.error_type == "sql_repair_exhausted"
