import copy
import json
from unittest.mock import patch

import pytest

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.intent_compiler import compile_intent_plan, CompilationContext
from AnomalyGraphAgent.planner import GlobalPlanner, PlannerFallbackError, SINGLE_PASS_SYSTEM_PROMPT
from AnomalyGraphAgent.sql_validation import validate_sql, SQLValidationError
from AnomalyGraphAgent.types import EnvironmentSnapshot, SchemaInfo, ExperimentRequest


WHITELIST = {"district": ["d_id", "d_ytd"], "customer": ["id", "district_id"]}


def setup(window=15):
    cfg = RuntimeConfig(openai_api_key="private-token")
    req = ExperimentRequest(target_database="lab", target_path=["hot_update", "lock_contention", "slow_query", "qps_drop"],
                            injected_nodes=["hot_update"], max_duration_sec=30,
                            workload={"enabled": True, "benchmark": "tpcc", "database": "lab", "injection_observe_sec": window})
    snap = EnvironmentSnapshot(database="lab", schema=SchemaInfo(database="lab", tables={
        t: {"columns": [{"name": c} for c in cols]} for t, cols in WHITELIST.items()}),
        db_metrics={"max_connections": 200, "Threads_connected": 4}, os_metrics={})
    payload = {"intents": [{"node": "hot_update", "strategy": "holder_waiter_same_hot_rows",
        "target": {"table": "district", "predicate": "d_id = 1"},
        "load": {"concurrency": 8, "duration_sec": 30, "hold_sec": 10},
        "sql": {"holder_sql": "SELECT * FROM district WHERE d_id = 1 FOR UPDATE",
                "waiter_sql": "UPDATE district SET d_ytd = d_ytd + 1 WHERE d_id = 1"},
        "expected_evidence": ["lock_contention", "slow_query", "qps_drop"]}]}
    return cfg, req, snap, payload


@pytest.mark.parametrize("window", [15, 30])
def test_one_call_lock_plan_and_strict_intent(window):
    cfg, req, snap, payload = setup(window)
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}) as llm, \
         patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"type": "const"}]}) as explain, \
         patch("AnomalyGraphAgent.executor.Executor.execute") as executor:
        dag, _, _ = planner.plan(req, snap)
    assert llm.call_count == 1 and explain.call_count == 2
    executor.assert_not_called()
    assert next(iter(dag.tasks.values())).actions[0]["duration_sec"] == window
    assert planner.last_plan_payload["planner_mode"] == "intent_compiler_v1"
    assert planner.last_plan_payload["planner_intent"] == payload
    assert llm.call_args.kwargs["response_format"]["json_schema"]["strict"] is True
    assert "task_id" not in SINGLE_PASS_SYSTEM_PROMPT
    assert "scripts" not in SINGLE_PASS_SYSTEM_PROMPT
    assert "runtime_paths" not in planner.last_planner_context


@pytest.mark.parametrize("sql", [
    "SELECT d.d_id AS id FROM lab.district AS d WHERE d.d_ytd > 1",
    "WITH x AS (SELECT d_id FROM district) SELECT x.d_id FROM x",
    "SELECT x.d_id FROM (SELECT d_id FROM district) x",
    "SELECT d.d_id FROM district d WHERE EXISTS (SELECT 1 FROM customer c WHERE c.district_id=d.d_id)",
    "SELECT d_id AS result FROM district ORDER BY result",
    "UPDATE lab.district d SET d.d_ytd=d.d_ytd+1 WHERE d.d_id=1",
    "INSERT INTO district (d_id,d_ytd) VALUES (1, 2)",
    "DELETE FROM district WHERE d_id=1",
])
def test_scope_aware_sql(sql):
    assert validate_sql(sql, "lab", WHITELIST)["explain"]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM other.district", "SELECT * FROM `other`.`district`",
    "WITH x AS (SELECT * FROM other.district) SELECT * FROM x",
    "SELECT * FROM (SELECT * FROM other.district) x",
    "SELECT d.absent FROM district d", "SELECT district_id FROM district",
    "WITH x AS (SELECT d_id FROM district) SELECT x.absent FROM x",
    "SELECT x.absent FROM (SELECT d_id FROM district) x",
    "SELECT d_id FROM district WHERE absent=1",
    "SELECT * FROM district d WHERE EXISTS (SELECT absent FROM customer c WHERE c.id=d.d_id)",
    "SELECT * FROM district; DROP TABLE district", "SET GLOBAL slow_query_log=OFF",
    "SELECT * INTO OUTFILE '/tmp/x' FROM district", "SELECT LOAD_FILE('/tmp/x')",
    "SELECT my_custom_function(d_id) FROM district", "UPDATE district SET d_ytd=1",
    "UPDATE district SET d_ytd=1 WHERE 1=1", "INSERT INTO district (absent) VALUES (1)",
    "ALTER TABLE district FORCE", "CALL lab.proc()",
    "SELECT d_id FROM district WHERE d_id =", "SELECT d_id FROM district WHERE",
])
def test_unsafe_or_unknown_sql(sql):
    with pytest.raises(SQLValidationError):
        validate_sql(sql, "lab", WHITELIST)


@pytest.mark.parametrize("mutation", ["node", "column", "database", "bad_target"])
def test_invalid_intent_never_reaches_explain_or_execution(mutation):
    cfg, req, snap, payload = setup()
    value = payload["intents"][0]
    if mutation == "node":
        value["node"] = "lock_contention"
    elif mutation == "bad_target":
        value["target"]["table"] = "unknown"
    elif mutation == "database":
        value["sql"]["holder_sql"] = "SELECT * FROM other.district WHERE d_id=1 FOR UPDATE"
    else:
        value["sql"]["waiter_sql"] = "UPDATE district SET missing=2 WHERE d_id=1"
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}), \
         patch("AnomalyGraphAgent.tools.explain_sql") as explain, \
         patch("AnomalyGraphAgent.executor.Executor.execute") as executor:
        with pytest.raises(PlannerFallbackError):
            GlobalPlanner(cfg).plan(req, snap)
    explain.assert_not_called()
    executor.assert_not_called()


def test_validate_all_sql_before_first_explain():
    cfg, req, snap, payload = setup()
    compiled = compile_intent_plan(payload, CompilationContext(req, cfg, "run"))
    compiled.task_specs[0].actions[0]["scripts"][1]["steps"][1]["sql"] = "UPDATE district SET absent=1 WHERE d_id=1"
    with patch("AnomalyGraphAgent.planner.compile_intent_plan", return_value=compiled), \
         patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}), \
         patch("AnomalyGraphAgent.tools.explain_sql") as explain:
        with pytest.raises(PlannerFallbackError):
            GlobalPlanner(cfg).plan(req, snap)
    explain.assert_not_called()


def test_serial_actions_over_window_rejected_locally():
    cfg, req, snap, payload = setup()
    compiled = compile_intent_plan(payload, CompilationContext(req, cfg, "run"))
    compiled.task_specs[0].actions.append(copy.deepcopy(compiled.task_specs[0].actions[0]))
    with pytest.raises(ValueError, match="Timing validation"):
        GlobalPlanner(cfg)._prepare_payload(compiled.to_dict(), req, snap, None)


def test_control_statements_never_explained_and_set_is_rejected():
    for sql in ("BEGIN", "ROLLBACK", "COMMIT", "LOCK TABLES district WRITE", "UNLOCK TABLES"):
        assert not validate_sql(sql, "lab", WHITELIST, controls=True)["explain"]
    with pytest.raises(SQLValidationError):
        validate_sql("SET GLOBAL long_query_time=0", "lab", WHITELIST, controls=True)


def test_missing_column_schema_is_not_assumed_valid():
    with pytest.raises(SQLValidationError, match="schema unavailable"):
        validate_sql("SELECT * FROM district", "lab", {"district": []})


def test_cte_intent_reaches_planner():
    cfg, req, snap, payload = setup()
    req.injected_nodes = ["improper_sql"]
    req.target_path = ["improper_sql", "slow_query", "qps_drop"]
    payload["intents"] = [{"node": "improper_sql", "strategy": "non_sargable_filter",
        "target": {"tables": ["district"], "columns": ["d_id"]},
        "load": {"duration_sec": 10, "concurrency": 4},
        "sql": {"query": "WITH x AS (SELECT d_id FROM district) SELECT x.d_id FROM x"},
        "expected_evidence": ["slow_query"]}]
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}) as llm, \
         patch("AnomalyGraphAgent.tools.explain_sql", return_value={"plan": [{"type": "ALL"}]}):
        dag, _, _ = GlobalPlanner(cfg).plan(req, snap)
    assert llm.call_count == 1
    assert next(iter(dag.tasks.values())).actions[0]["sql"].startswith("WITH")


def test_explain_repair_returns_intent_not_taskspec():
    cfg, req, snap, payload = setup()
    repaired = copy.deepcopy(payload)
    repaired["intents"][0]["sql"]["waiter_sql"] = "UPDATE district SET d_ytd = d_ytd + 2 WHERE d_id = 1"
    planner = GlobalPlanner(cfg)
    plans = [{"plan": []}, {"plan": [{"error": "optimizer rejected SQL"}]}, {"plan": []}, {"plan": []}]
    with patch("AnomalyGraphAgent.tools.llm_generate", side_effect=[{"json_payload": payload}, {"json_payload": repaired}]) as llm, \
         patch("AnomalyGraphAgent.tools.explain_sql", side_effect=plans):
        planner.plan(req, snap)
    assert llm.call_count == 2
    repair = json.loads(llm.call_args.kwargs["user_prompt"])
    assert repair["source"] == "intents[0].sql.waiter_sql"
    assert "task_specs" not in repair
    assert planner.last_plan_payload["planner_intent"] == repaired


def test_serial_dependencies_exceed_window():
    cfg, req, snap, payload = setup()
    req.injected_nodes.append("long_tx")
    other = copy.deepcopy(payload["intents"][0])
    other.update(node="long_tx", strategy="long_transaction_hot_rows")
    payload["intents"].append(other)
    compiled = compile_intent_plan(payload, CompilationContext(req, cfg, "run"))
    a, b = [s.task_id for s in compiled.task_specs]
    compiled.dependencies = [[a, b]]
    with pytest.raises(ValueError, match="Timing validation"):
        GlobalPlanner(cfg)._prepare_payload(compiled.to_dict(), req, snap, None)


def test_resource_uid_not_rewritten_by_safety():
    cfg, req, snap, _ = setup()
    req.injected_nodes = ["resource_cpu"]
    req.target_path = ["resource_cpu"]
    payload = {"intents": [{"node": "resource_cpu", "strategy": "cpu_pressure", "target": None,
                           "load": {"duration_sec": 15, "intensity": "low"}, "sql": None, "expected_evidence": []}]}
    ctx = CompilationContext(req, cfg, "fixed-resource-run", resource_profiles={"resource_cpu": {"low": {"percent": 40}}})
    planner = GlobalPlanner(cfg)
    with patch("AnomalyGraphAgent.tools.llm_generate", return_value={"json_payload": payload}):
        dag, _, _ = planner.plan(req, snap, compilation_context=ctx)
    action = next(iter(dag.tasks.values())).actions[0]
    assert action == planner.last_plan_payload["compilation"]["task_specs"][0]["actions"][0]
    assert action["cleanup_command"][-1] == action["command"][-1]
