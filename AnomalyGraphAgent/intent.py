"""Intent v1 contract and static capability inventory; no execution or compilation."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from AnomalyGraphAgent.graph import ANOMALY_GRAPH
from AnomalyGraphAgent.types import ExperimentRequest


INTENT_VERSION = "1"

# Nodes implemented by the local v1 compiler. Capability prerequisites still apply.
COMPILED_NODES = frozenset({
    "traffic_surge", "backup", "hot_update", "long_tx", "missing_index", "improper_sql",
    "large_temp_table", "resource_cpu", "resource_io", "resource_memory",
    "disk_full_or_pressure", "metadata_lock", "table_lock",
})


class IntentValidationError(ValueError):
    """A contract violation, with a stable field path for future repair calls."""

    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "string"
    nullable: bool = False
    minimum: float | None = None
    choices: tuple[str, ...] = ()
    default: Any = None

    def schema(self) -> dict:
        result: dict[str, Any] = {
            "type": [self.kind, "null"] if self.nullable else self.kind,
        }
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.choices:
            result["enum"] = list(self.choices) + ([None] if self.nullable else [])
        if self.kind == "string":
            result["minLength"] = 1
        if self.kind == "array":
            result.update(items={"type": "string", "minLength": 1}, minItems=1)
        return result


@dataclass(frozen=True)
class Strategy:
    node: str
    name: str
    target: tuple[Field, ...]
    load: tuple[Field, ...]
    sql: tuple[Field, ...]
    action_kinds: tuple[str, ...]
    capability: str
    requirements: tuple[str, ...]
    evidence: tuple[str, ...]
    source: str

    @property
    def defaults(self) -> dict:
        # Defaults are declarations for the compiler, never applied by validation.
        return {
            f"{group}.{field.name}": field.default
            for group in ("target", "load", "sql")
            for field in getattr(self, group)
            if field.default is not None
        }

    def schema(self) -> dict:
        properties = {
            "node": {"type": "string", "enum": [self.node]},
            "strategy": {"type": "string", "enum": [self.name]},
        }
        for group in ("target", "load", "sql"):
            fields = getattr(self, group)
            properties[group] = (
                _object_schema({field.name: field.schema() for field in fields})
                if fields else {"type": "null"}
            )
        properties["expected_evidence"] = {
            "type": "array", "items": {"type": "string", "enum": list(self.evidence)},
        }
        return _object_schema(properties)


def _object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


DURATION = Field("duration_sec", "number", minimum=0)
CONCURRENCY = Field("concurrency", "integer", nullable=True, minimum=1, default=4)
LOCK_CONCURRENCY = Field("concurrency", "integer", minimum=2)
HOLD = Field("hold_sec", "number", minimum=0)
INTENSITY = Field("intensity", choices=("low", "medium", "high"))
TABLE = (Field("table"),)
HOT_TARGET = TABLE + (Field("predicate"),)
QUERY_TARGET = (Field("tables", "array"), Field("columns", "array"))
QUERY_LOAD = (DURATION, CONCURRENCY)
LOCK_LOAD = (DURATION, LOCK_CONCURRENCY, HOLD)
QUERY_SQL = (Field("query"),)
LOCK_SQL = (Field("holder_sql"), Field("waiter_sql"))
SQL_SOURCE = "AnomalyGraphAgent/executor.py:_run_sql_workload"
TX_SOURCE = "AnomalyGraphAgent/executor.py:_run_raw_transaction_script"
RESOURCE_SOURCE = "AnomalyGraphAgent/tools.py:canonicalize_resource_chaosblade_action"


def _strategies() -> tuple[Strategy, ...]:
    definitions = [
        Strategy("traffic_surge", "increase_client_concurrency", (),
                 (DURATION, Field("concurrency", "integer", minimum=1),
                  Field("rate", "integer", nullable=True, minimum=1)), (),
                 ("benchbase_burst",), "conditional",
                 ("Validated active BenchBase profile; null rate inherits its rate/mix.",),
                 ("threads_concurrency_up", "qps_drop"),
                 "AnomalyGraphAgent/executor.py:_run_benchbase_burst"),
        Strategy("backup", "large_table_logical_backup",
                 TABLE + (Field("predicate", nullable=True),), (DURATION,), (),
                 ("logical_backup_command",), "conditional",
                 ("Configured mysqldump; request database; bounded output and cleanup.",),
                 ("disk_io_up", "query_latency_up", "qps_drop"),
                 "AnomalyGraphAgent/executor.py:_run_raw_command"),
    ]
    for node, name in (("hot_update", "holder_waiter_same_hot_rows"),
                       ("long_tx", "long_transaction_hot_rows")):
        definitions.append(Strategy(
            node, name, HOT_TARGET, LOCK_LOAD, LOCK_SQL,
            ("raw_transaction_script",), "conditional",
            ("Verify existing shared hot rows and per-role concurrency allocation.",
             "Holder-before-waiter timing must be validated; executor has no readiness barrier."),
            ("lock_contention", "slow_query", "qps_drop", "timeout"), TX_SOURCE))
    for node, name, evidence in (
        ("missing_index", "unindexed_filter", "poor_plan"),
        ("improper_sql", "non_sargable_filter", "full_scan"),
        ("large_temp_table", "sort_group_join_spill", "temp_table_spill"),
    ):
        definitions.append(Strategy(
            node, name, QUERY_TARGET, QUERY_LOAD, QUERY_SQL,
            ("raw_sql_workload",), "conditional",
            ("Read-only query; verify schema, indexes and EXPLAIN strategy evidence.",),
            (evidence, "high_query_latency", "slow_query", "qps_drop"), SQL_SOURCE))
    for node, name in (("resource_cpu", "cpu_pressure"),
                       ("resource_io", "disk_io_pressure"),
                       ("resource_memory", "memory_pressure"),
                       ("disk_full_or_pressure", "bounded_disk_pressure")):
        definitions.append(Strategy(
            node, name, (), (DURATION, INTENSITY), (), ("raw_command",),
            "conditional", ("Platform/tool preflight and approved intensity mapping required.",
                            "Disk strategies use bounded burn only; no disk-fill substitution."),
            ("cpu_usage_up", "disk_io_up", "memory_usage_up", "query_latency_up", "qps_drop"),
            RESOURCE_SOURCE))
    for node in ("resource_network", "network_latency"):
        definitions.append(Strategy(
            node, "mysql_network_delay", (),
            (DURATION, Field("delay_ms", "number", minimum=0)), (),
            ("raw_command",), "blocked",
            ("Current canonicalizer forces network drop; delay cannot be compiled losslessly.",
             "Requires platform delay support and request-derived MySQL network scope."),
            ("network_stall", "timeout", "qps_drop"), RESOURCE_SOURCE))
    for node, name, target, sql, requirement in (
        ("metadata_lock", "transaction_blocks_ddl", TABLE, LOCK_SQL,
         "Reversible DDL validation, holder ordering and bounded MDL wait required."),
        ("table_lock", "explicit_table_lock", TABLE, (Field("waiter_sql"),),
         "LOCK/UNLOCK must use the same holder connection; validate waiter ordering."),
    ):
        definitions.append(Strategy(
            node, name, target, LOCK_LOAD, sql, ("raw_transaction_script",),
            "conditional", (requirement,),
            ("metadata_lock_wait", "lock_contention", "lock_wait", "slow_query", "qps_drop"), TX_SOURCE))
    definitions.extend((
        Strategy("deadlock_storm", "opposite_order_updates", TABLE, LOCK_LOAD,
                 (Field("first_update"), Field("second_update")),
                 ("raw_transaction_script",), "blocked",
                 ("Executor runs each role once; sustained deadlock pairs require repeat/retry support.",),
                 ("deadlock_detected", "write_throughput_drop"), TX_SOURCE),
        Strategy("redo_log_pressure", "committed_write_pressure", QUERY_TARGET,
                 QUERY_LOAD, (Field("write_sql"),), ("raw_transaction_script",), "blocked",
                 ("SQL workload has no explicit commit; transaction scripts have no duration loop.",),
                 ("redo_log_flush_stall", "commit_latency_up", "write_throughput_drop"), TX_SOURCE),
        Strategy("connection_storm", "short_connection_burst", (), QUERY_LOAD, (),
                 ("raw_sql_workload",), "blocked",
                 ("SQL workload reuses one connection per worker; reconnect loop is missing.",),
                 ("connection_pressure", "connection_error", "timeout", "qps_drop"), SQL_SOURCE),
        Strategy("excessive_index", "redundant_index_write_amplification", QUERY_TARGET,
                 QUERY_LOAD, (Field("write_sql"),), ("raw_sql_workload",), "disabled",
                 ("Graph disables this node; index amplification evidence is not established.",),
                 ("write_throughput_drop", "slow_query", "qps_drop"),
                 "AnomalyGraphAgent/graph.py:NODES[excessive_index]"),
    ))
    return tuple(definitions)


STRATEGIES = MappingProxyType({(s.node, s.name): s for s in _strategies()})


def strategies_for(node: str) -> tuple[Strategy, ...]:
    return tuple(s for s in STRATEGIES.values() if s.node == node)


def _validate_nodes(nodes: list[str]) -> None:
    if not isinstance(nodes, list) or not nodes:
        raise IntentValidationError("injected_nodes", "must be a non-empty array")
    seen = set()
    for index, node in enumerate(nodes):
        path = f"injected_nodes[{index}]"
        if not isinstance(node, str) or node not in ANOMALY_GRAPH.nodes:
            raise IntentValidationError(path, "unknown root node")
        if not ANOMALY_GRAPH.nodes[node].injectable:
            raise IntentValidationError(path, "node is not injectable")
        if node in seen:
            raise IntentValidationError(path, "duplicate root node")
        if not strategies_for(node):
            raise IntentValidationError(path, "no registered strategy")
        seen.add(node)


def build_intent_schema(injected_nodes: list[str]) -> dict:
    """Request-specific schema; cross-root uniqueness remains a local constraint."""
    _validate_nodes(injected_nodes)
    return _object_schema({"intents": {
        "type": "array", "minItems": len(injected_nodes), "maxItems": len(injected_nodes),
        "items": {"anyOf": [s.schema() for node in injected_nodes for s in strategies_for(node)]},
    }})


def _check(value: Any, schema: dict, path: str) -> None:
    """Validate the small schema subset emitted above from the same field definitions."""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                _check(value, branch, path)
                return
            except IntentValidationError:
                pass
        raise IntentValidationError(path, "does not match an allowed strategy")
    types = schema["type"]
    types = types if isinstance(types, list) else [types]
    actual = ("null" if value is None else "boolean" if isinstance(value, bool)
              else "string" if isinstance(value, str) else "object" if isinstance(value, dict)
              else "array" if isinstance(value, list) else "integer" if isinstance(value, int)
              else "number" if isinstance(value, float) else "invalid")
    if actual not in types and not (actual == "integer" and "number" in types):
        raise IntentValidationError(path, f"expected {types}, got {actual}")
    if "enum" in schema and value not in schema["enum"]:
        raise IntentValidationError(path, "value is not allowed")
    if actual == "null":
        return
    if actual in {"number", "integer"}:
        if isinstance(value, float) and not math.isfinite(value):
            raise IntentValidationError(path, "number must be finite")
        if value < schema.get("minimum", -math.inf):
            raise IntentValidationError(path, "below minimum")
    if actual == "string" and not value.strip():
        raise IntentValidationError(path, "must not be blank")
    if actual == "object":
        properties = schema["properties"]
        if set(value) != set(properties):
            raise IntentValidationError(path, "missing or extra fields")
        for key, child in properties.items():
            _check(value[key], child, f"{path}.{key}")
    if actual == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise IntentValidationError(path, "invalid array length")
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{index}]")


def validate_intent_plan(payload: Any, injected_nodes: list[str]) -> dict:
    """Validate contract only, not runtime feasibility, SQL, or execution safety."""
    _validate_nodes(injected_nodes)
    if not isinstance(payload, dict) or set(payload) != {"intents"}:
        raise IntentValidationError("$", "expected object containing only intents")
    intents = payload["intents"]
    if not isinstance(intents, list):
        raise IntentValidationError("intents", "expected array")
    seen = set()
    for index, intent in enumerate(intents):
        path = f"intents[{index}]"
        if not isinstance(intent, dict):
            raise IntentValidationError(path, "expected object")
        node, name = intent.get("node"), intent.get("strategy")
        if not isinstance(node, str) or node not in injected_nodes:
            raise IntentValidationError(f"{path}.node", "root is not requested")
        if node in seen:
            raise IntentValidationError(f"{path}.node", "duplicate root")
        if not isinstance(name, str) or (node, name) not in STRATEGIES:
            raise IntentValidationError(f"{path}.strategy", "unknown strategy for root")
        _check(intent, STRATEGIES[node, name].schema(), path)
        load = intent["load"]
        for key in ("duration_sec", "hold_sec", "delay_ms"):
            if key in load and load[key] <= 0:
                raise IntentValidationError(f"{path}.load.{key}", "must be positive")
        if load.get("hold_sec", 0) > load["duration_sec"]:
            raise IntentValidationError(f"{path}.load.hold_sec", "exceeds duration_sec")
        seen.add(node)
    if seen != set(injected_nodes):
        raise IntentValidationError("intents", "missing requested roots")
    return copy.deepcopy(payload)


def require_compilable_strategy(node: str, strategy: str) -> Strategy:
    """Static guard for the future compiler; conditional still requires preflight."""
    _validate_nodes([node])
    spec = STRATEGIES.get((node, strategy))
    if spec is None:
        raise IntentValidationError("strategy", "unknown strategy for root")
    if spec.capability != "conditional":
        raise IntentValidationError("strategy", "; ".join(spec.requirements))
    return spec


def request_capability_matrix(input_dir: str | Path) -> list[dict]:
    """Inventory request_*.json only; historical plan artifacts are not requests."""
    rows = []
    for path in sorted(Path(input_dir).glob("request_*.json")):
        request = ExperimentRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for node in request.injected_nodes:
            graph_node = ANOMALY_GRAPH.node(node)
            specs = strategies_for(node)
            rows.append({
                "request": path.name, "node": node,
                "injectable": bool(graph_node and graph_node.injectable),
                "registered": bool(specs), "compiler_implemented": node in COMPILED_NODES,
                "strategies": [{"strategy": s.name, "capability": s.capability,
                                "action_kinds": list(s.action_kinds),
                                "requirements": list(s.requirements), "source": s.source,
                                "defaults": s.defaults} for s in specs],
            })
    return rows
