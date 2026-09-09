import ast
import copy
import json
from pathlib import Path

import pytest

from AnomalyGraphAgent.graph import ANOMALY_GRAPH
from AnomalyGraphAgent.intent import (
    COMPILED_NODES, STRATEGIES, IntentValidationError, build_intent_schema,
    request_capability_matrix, require_compilable_strategy,
    strategies_for, validate_intent_plan,
)
from AnomalyGraphAgent.types import ExperimentRequest
from AnomalyGraphAgent.runtime import DBMAGSRuntime


ROOT = Path(__file__).resolve().parents[1]


def example(spec):
    groups = {}
    for group in ("target", "load", "sql"):
        fields = getattr(spec, group)
        groups[group] = {
            f.name: (None if f.nullable else f.choices[0] if f.choices
                     else ["customer"] if f.kind == "array"
                     else 15 if f.name == "duration_sec"
                     else 4 if f.kind in {"integer", "number"}
                     else "customer") for f in fields
        } if fields else None
    return {"node": spec.node, "strategy": spec.name, **groups,
            "expected_evidence": list(spec.evidence)}


def hot_plan():
    return {"intents": [example(strategies_for("hot_update")[0])]}


def test_inventory_covers_every_request_and_reports_disabled():
    paths = sorted((ROOT / "anomalychainInput").glob("request_*.json"))
    assert paths
    expected = []
    for path in paths:
        request = ExperimentRequest.from_dict(json.loads(path.read_text()))
        assert request.injected_nodes, path
        assert len(request.injected_nodes) == len(set(request.injected_nodes))
        for node in request.injected_nodes:
            assert node in ANOMALY_GRAPH.nodes
            assert strategies_for(node), (path, node)
            expected.append((path.name, node))
    rows = request_capability_matrix(ROOT / "anomalychainInput")
    assert [(r["request"], r["node"]) for r in rows] == expected
    assert all(r["registered"] for r in rows)
    assert all(r["compiler_implemented"] == (r["node"] in COMPILED_NODES) for r in rows)
    disabled = [r for r in rows if not r["injectable"]]
    assert {r["node"] for r in disabled} == {"excessive_index"}
    assert disabled[0]["strategies"][0]["capability"] == "disabled"


@pytest.mark.parametrize("spec", list(STRATEGIES.values()), ids=lambda s: s.node)
def test_each_strategy_contract_and_executor_dispatch(spec):
    assert ANOMALY_GRAPH.node(spec.node) is not None
    tree = ast.parse((ROOT / "AnomalyGraphAgent/executor.py").read_text())
    dispatch = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_action")
    literals = {n.value for n in ast.walk(dispatch)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert set(spec.action_kinds) <= literals
    assert spec.requirements and spec.source
    payload = {"intents": [example(spec)]}
    if spec.capability == "disabled":
        with pytest.raises(IntentValidationError, match="not injectable"):
            validate_intent_plan(payload, [spec.node])
        return
    original = copy.deepcopy(payload)
    result = validate_intent_plan(payload, [spec.node])
    assert result == original == payload
    assert result is not payload
    assert build_intent_schema([spec.node])["properties"]["intents"]["items"]["anyOf"] == [spec.schema()]


@pytest.mark.parametrize("node", ["lock_contention", "excessive_index", "not_a_node"])
def test_invalid_roots_are_not_mapped(node):
    with pytest.raises(IntentValidationError):
        build_intent_schema([node])
    payload = hot_plan()
    payload["intents"][0]["node"] = node
    with pytest.raises(IntentValidationError):
        validate_intent_plan(payload, ["hot_update"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"),
                                  0, -1, True, "15", None])
def test_invalid_duration(bad):
    payload = hot_plan()
    payload["intents"][0]["load"]["duration_sec"] = bad
    with pytest.raises(IntentValidationError):
        validate_intent_plan(payload, ["hot_update"])


@pytest.mark.parametrize("group,key,value", [
    (None, "task_id", "legacy"), (None, "strategy", "cpu_pressure"),
    ("load", "intensity", "high"), ("target", "database", "other"),
    ("sql", "query", "SELECT 1"), ("load", "concurrency", True),
    ("load", "concurrency", 1), ("load", "hold_sec", 16),
    ("target", "table", " "), (None, "node", []),
])
def test_invalid_or_cross_strategy_fields(group, key, value):
    payload = hot_plan()
    intent = payload["intents"][0]
    (intent[group] if group else intent)[key] = value
    with pytest.raises(IntentValidationError):
        validate_intent_plan(payload, ["hot_update"])


@pytest.mark.parametrize("payload", [[], None, "text", {}, {"intents": []},
                                     {"intents": ["text"]}, {"intents": {}}])
def test_malformed_plan(payload):
    with pytest.raises(IntentValidationError):
        validate_intent_plan(payload, ["hot_update"])


def test_missing_duplicate_and_multiple_roots():
    payload = hot_plan()
    with pytest.raises(IntentValidationError, match="missing"):
        validate_intent_plan(payload, ["hot_update", "resource_cpu"])
    payload["intents"].append(copy.deepcopy(payload["intents"][0]))
    with pytest.raises(IntentValidationError, match="duplicate"):
        validate_intent_plan(payload, ["hot_update"])
    with pytest.raises(IntentValidationError, match="duplicate"):
        build_intent_schema(["hot_update", "hot_update"])
    payload["intents"][1] = example(strategies_for("resource_cpu")[0])
    assert validate_intent_plan(payload, ["resource_cpu", "hot_update"]) == payload
    del payload["intents"][0]["sql"]["holder_sql"]
    with pytest.raises(IntentValidationError, match="missing"):
        validate_intent_plan(payload, ["hot_update", "resource_cpu"])


def test_schema_closed_and_defaults_not_silently_applied():
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    nodes = [n.node_id for n in ANOMALY_GRAPH.injectable_nodes()]
    walk(build_intent_schema(nodes))
    spec = strategies_for("improper_sql")[0]
    assert spec.defaults == {"load.concurrency": 4}
    payload = {"intents": [example(spec)]}
    assert validate_intent_plan(payload, [spec.node])["intents"][0]["load"]["concurrency"] is None
    spec.defaults["load.concurrency"] = 99
    assert spec.defaults["load.concurrency"] == 4


@pytest.mark.parametrize("node", ["network_latency", "resource_network", "deadlock_storm",
                                   "connection_storm", "redo_log_pressure", "excessive_index"])
def test_unsupported_semantics_never_silently_substituted(node):
    spec = strategies_for(node)[0]
    with pytest.raises(IntentValidationError):
        require_compilable_strategy(node, spec.name)
    assert require_compilable_strategy("hot_update", "holder_waiter_same_hot_rows").node == "hot_update"


def test_planner_contract_artifacts_include_versions_and_failure_fields(tmp_path):
    runtime = DBMAGSRuntime.__new__(DBMAGSRuntime)
    runtime.planner = type("Planner", (), {
        "last_plan_payload": {}, "planner_llm_calls": 1,
        "planner_json_repair_attempts": 1, "sql_repair_attempts": 2,
        "_llm_usage": [{"usage": {"total_tokens": 4}}],
        "_planner_timings": {"llm_generate_plan": 0.2},
    })()
    runtime._write_json = lambda path, data: path.write_text(json.dumps(data, ensure_ascii=False))
    round_dir = tmp_path / "round_1"
    round_dir.mkdir()
    request = ExperimentRequest(target_database="lab", target_path=["hot_update"], injected_nodes=["hot_update"])
    runtime._write_planner_contract_artifacts(round_dir, request, status="failed", error=type("E", (), {
        "stage": "sql_explain", "error_type": "database_connection_error", "reason": "offline",
        "context": {"validation_history": [{"status": "failed"}]},
    })())
    for name in ("planner_intent.json", "compiled_task_specs.json", "intent_validation.json", "compilation_trace.json"):
        data = json.loads((round_dir / name).read_text())
        assert data["status"] == "failed"
        assert data["error_type"] == "database_connection_error"
        assert data["intent_schema_version"]
        assert data["compiler_version"]
        assert data["planner_llm_calls"] == 1
