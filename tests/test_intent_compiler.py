import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.executor import Executor
from AnomalyGraphAgent.intent import STRATEGIES, strategies_for
from AnomalyGraphAgent.intent_compiler import CompilationContext, IntentCompilationError, compile_intent_plan
from AnomalyGraphAgent.planner import GlobalPlanner
from AnomalyGraphAgent.types import ExperimentRequest


def intent(node):
    strategy = strategies_for(node)[0]
    values = {}
    for group in ("target", "load", "sql"):
        fields = getattr(strategy, group)
        values[group] = {f.name: (None if f.nullable else f.choices[0] if f.choices
                                  else ["district"] if f.kind == "array"
                                  else 30 if f.name == "duration_sec"
                                  else 8 if f.name == "concurrency"
                                  else 10 if f.kind == "number" else "district")
                         for f in fields} if fields else None
    result = dict(node=node, strategy=strategy.name, **values, expected_evidence=[])
    if node in {"hot_update", "long_tx"}:
        result["target"]["predicate"] = "d_id = 1"
        result["sql"] = {"holder_sql": "SELECT * FROM district WHERE d_id = 1 FOR UPDATE",
                         "waiter_sql": "UPDATE district SET d_ytd = d_ytd + 1 WHERE d_id = 1"}
    elif node == "metadata_lock":
        result["sql"] = {"holder_sql": "SELECT * FROM district", "waiter_sql": "ALTER TABLE district FORCE"}
    elif node == "table_lock":
        result["sql"] = {"waiter_sql": "SELECT * FROM district"}
    elif node in {"improper_sql", "missing_index", "large_temp_table"}:
        result["sql"] = {"query": "SELECT * FROM district WHERE d_ytd > 1"}
    return result


def context(node):
    return CompilationContext(
        request=ExperimentRequest(target_database="tpcc_10W", injected_nodes=[node], max_duration_sec=30,
                                  workload={"enabled": True, "benchmark": "tpcc", "database": "tpcc_10W",
                                            "injection_observe_sec": 15}),
        config=RuntimeConfig(), run_id="run-one", mysqldump_path="/usr/bin/mysqldump",
        mysql_defaults_file="/tmp/private-client.cnf",
        benchbase_profile={"benchmark": "tpcc", "database": "tpcc_10W", "config_path": "/tmp/tpcc.xml",
                           "terminals": 4, "rate": 400, "duration_sec": 30,
                           "transaction_mix": {"NewOrder": 45, "Payment": 55},
                           "mix_template": "baseline", "rationale": "Background mix"},
        transaction_types=("NewOrder", "Payment"),
        resource_profiles={n: {"low": {"percent": 40, "size": "32M", "path": "/tmp/dbmags"}}
                           for n in ("resource_cpu", "resource_memory", "resource_io", "disk_full_or_pressure")},
    )


SUPPORTED = [s.node for s in STRATEGIES.values() if s.capability == "conditional"]


@pytest.mark.parametrize("node", SUPPORTED)
def test_compile_direct_validation_and_mock_executor_dispatch(node):
    ctx = context(node)
    payload = {"intents": [intent(node)]}
    original = copy.deepcopy(payload)
    with patch("subprocess.Popen", side_effect=AssertionError("process side effect")), \
         patch("pymysql.connect", side_effect=AssertionError("database side effect")), \
         patch("AnomalyGraphAgent.tools.llm_generate", side_effect=AssertionError("LLM side effect")):
        result = compile_intent_plan(payload, ctx)
    assert original == payload
    assert result.to_dict() == compile_intent_plan(payload, ctx).to_dict()
    spec = result.task_specs[0].to_dict()
    GlobalPlanner(ctx.config)._validate_direct_task_spec(spec, ctx.request)
    assert spec["metadata"]["root_cause"] == node
    assert spec["actions"][0]["duration_sec"] == 15
    executor = object.__new__(Executor)
    methods = {"raw_sql_workload": "_run_sql_workload", "raw_transaction_script": "_run_raw_transaction_script",
               "raw_command": "_run_raw_command", "logical_backup_command": "_run_raw_command",
               "benchbase_burst": "_run_benchbase_burst"}
    action = spec["actions"][0]
    method = methods[action["kind"]]
    setattr(executor, method, Mock(return_value={"ok": True}))
    assert executor._run_action(action["kind"], action, spec["task_id"]) == {"ok": True}
    getattr(executor, method).assert_called_once()


def test_hot_holder_waiter_connections_and_rollback():
    ctx = context("hot_update")
    action = compile_intent_plan({"intents": [intent("hot_update")]}, ctx).task_specs[0].actions[0]
    connections = []
    def connect(**kwargs):
        conn = Mock()
        connections.append(conn)
        return conn
    executor = object.__new__(Executor)
    executor.config = ctx.config
    with patch("pymysql.connect", side_effect=connect), patch("time.sleep"):
        result = executor._run_raw_transaction_script(action)
    assert len(connections) == 8
    sqls = [[c.args[0] for c in conn.cursor.return_value.execute.call_args_list] for conn in connections]
    assert sum(any("FOR UPDATE" in sql for sql in statements) for statements in sqls) == 1
    assert sum(any(sql.startswith("UPDATE") for sql in statements) for statements in sqls) == 7
    assert all(statements[-1] == "ROLLBACK" for statements in sqls)
    assert all(conn.close.called for conn in connections)
    assert result["executed_steps"] > 0


def test_uids_and_cleanup_are_scoped_to_run_round_and_attempt():
    ctx = context("resource_cpu")
    payload = {"intents": [intent("resource_cpu")]}
    uids = []
    for current in [ctx, replace(ctx, run_id="run-two"), replace(ctx, round_no=2), replace(ctx, compilation_id="2")]:
        action = compile_intent_plan(payload, current).task_specs[0].actions[0]
        uid = action["command"][action["command"].index("--uid") + 1]
        assert action["cleanup_command"] == [ctx.config.chaosblade_path, "destroy", uid]
        uids.append(uid)
    assert len(set(uids)) == 4


def test_backup_predicate_and_no_credentials_in_result():
    value = intent("backup")
    value["target"]["predicate"] = "d_id <= 100 AND d_name = 'hello world'"
    ctx = context("backup")
    ctx.config.mysql_password = "never-log-this"
    result = compile_intent_plan({"intents": [value]}, ctx)
    command = result.task_specs[0].actions[0]["command"]
    assert "--where=" + value["target"]["predicate"] in command
    assert command[1] == "--defaults-extra-file=/tmp/private-client.cnf"
    assert command[-2:] == ["tpcc_10W", "district"]
    assert "never-log-this" not in json.dumps(result.to_dict())


def test_benchbase_inheritance_default_and_source_map():
    ctx = context("traffic_surge")
    value = intent("traffic_surge")
    value["load"]["concurrency"] = 64
    result = compile_intent_plan({"intents": [value]}, ctx)
    profile = result.task_specs[0].actions[0]["profile"]
    assert profile["terminals"] == 64 and profile["rate"] == 400
    assert profile["transaction_mix"] == ctx.benchbase_profile["transaction_mix"]
    assert profile["duration_sec"] == 15
    ctx = context("improper_sql")
    result = compile_intent_plan({"intents": [intent("improper_sql")]}, ctx)
    assert result.task_specs[0].actions[0]["concurrency"] == 4
    assert result.source_map["task_specs[0].actions[0].sql"] == "intents[0].sql.query"
    assert any(n["reason"] == "strategy_default" for n in result.normalizations)


@pytest.mark.parametrize("node", [s.node for s in STRATEGIES.values() if s.capability != "conditional"])
def test_blocked_strategies_do_not_compile(node):
    with pytest.raises(IntentCompilationError):
        compile_intent_plan({"intents": [intent(node)]}, context(node))


def test_mismatch_ddl_and_missing_resource_profile_fail():
    value = intent("hot_update")
    value["sql"]["waiter_sql"] = value["sql"]["waiter_sql"].replace("= 1", "= 2")
    with pytest.raises(IntentCompilationError, match="identical"):
        compile_intent_plan({"intents": [value]}, context("hot_update"))
    value = intent("metadata_lock")
    value["sql"]["waiter_sql"] = "ALTER TABLE district DROP COLUMN d_name"
    with pytest.raises(IntentCompilationError, match="schema-preserving"):
        compile_intent_plan({"intents": [value]}, context("metadata_lock"))
    with pytest.raises(IntentCompilationError, match="resource profile"):
        compile_intent_plan({"intents": [intent("resource_cpu")]}, replace(context("resource_cpu"), resource_profiles={}))


def test_all_request_nodes_compile_or_have_explicit_capability_block():
    for path in (Path(__file__).resolve().parents[1] / "anomalychainInput").glob("request_*.json"):
        req = ExperimentRequest.from_dict(json.loads(path.read_text()))
        node = req.injected_nodes[0]
        if node in SUPPORTED:
            result = compile_intent_plan({"intents": [intent(node)]}, replace(context(node), request=req))
            assert result.task_specs[0].metadata["root_cause"] == node
        else:
            with pytest.raises(IntentCompilationError):
                compile_intent_plan({"intents": [intent(node)]}, replace(context(node), request=req))


def test_no_workload_limit_and_hold_clamp():
    ctx = context("hot_update")
    ctx.request.workload = {}
    value = intent("hot_update")
    value["load"].update(duration_sec=60, hold_sec=50)
    result = compile_intent_plan({"intents": [value]}, ctx)
    action = result.task_specs[0].actions[0]
    assert action["duration_sec"] == 30
    assert action["scripts"][0]["steps"][2] == {"sleep_sec": 30}
    assert action["concurrency"] == 8
    assert len(result.normalizations) == 2


def test_table_lock_same_connection_unlock_and_mdl_source_map():
    action = compile_intent_plan({"intents": [intent("table_lock")]}, context("table_lock")).task_specs[0].actions[0]
    holder = action["scripts"][0]
    assert holder["steps"][0] == {"sql": "LOCK TABLES `district` WRITE"}
    assert holder["steps"][-1] == {"sql": "UNLOCK TABLES"}
    result = compile_intent_plan({"intents": [intent("metadata_lock")]}, context("metadata_lock"))
    assert result.source_map["task_specs[0].actions[0].scripts[1].steps[0].sql"] == "intents[0].sql.waiter_sql"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
def test_invalid_context_duration_is_not_repaired(bad):
    ctx = context("hot_update")
    ctx.request.max_duration_sec = bad
    with pytest.raises(IntentCompilationError):
        compile_intent_plan({"intents": [intent("hot_update")]}, ctx)
