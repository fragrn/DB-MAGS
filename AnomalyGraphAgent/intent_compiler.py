"""Pure Intent-to-TaskSpec compilation. Does not probe, execute, or call an LLM."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.intent import (
    COMPILED_NODES, INTENT_VERSION, IntentValidationError, require_compilable_strategy, validate_intent_plan,
)
from AnomalyGraphAgent.tools import canonicalize_resource_chaosblade_action, validate_traffic_surge_profile
from AnomalyGraphAgent.types import ExperimentRequest, TaskSpec


COMPILER_VERSION = "1"


class IntentCompilationError(ValueError):
    pass


@dataclass(frozen=True)
class CompilationContext:
    request: ExperimentRequest
    config: RuntimeConfig
    run_id: str
    round_no: int = 1
    compilation_id: str = "1"
    benchbase_profile: dict[str, Any] | None = None
    transaction_types: tuple[str, ...] = ()
    mysqldump_path: str = ""
    mysql_defaults_file: str = ""
    resource_profiles: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


@dataclass
class CompilationResult:
    task_specs: list[TaskSpec] = field(default_factory=list)
    dependencies: list[list[str]] = field(default_factory=list)
    source_map: dict[str, str] = field(default_factory=dict)
    normalizations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"task_specs": [s.to_dict() for s in self.task_specs],
                "dependencies": copy.deepcopy(self.dependencies),
                "source_map": dict(self.source_map),
                "normalizations": copy.deepcopy(self.normalizations),
                "warnings": list(self.warnings)}


def _positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise IntentCompilationError(f"{label} must be a positive finite number")
    return value


def _absolute(value: str, label: str) -> str:
    if not value or not Path(value).is_absolute():
        raise IntentCompilationError(f"{label} must be an absolute path")
    return value


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise IntentCompilationError(f"Unsupported identifier: {value!r}")
    return f"`{value}`"


def _single_sql(sql: str) -> str:
    sql = sql.strip().removesuffix(";").strip()
    if any(token in sql for token in (";", "--", "/*", "#")):
        raise IntentCompilationError("Compiler accepts one uncommented SQL statement per slot")
    return sql


def _hot_sql(intent: dict) -> tuple[str, str]:
    """Only accept simple single-table, identical-predicate holder/waiter shapes."""
    table = re.escape(intent["target"]["table"])
    holder = _single_sql(intent["sql"]["holder_sql"])
    waiter = _single_sql(intent["sql"]["waiter_sql"])
    h = re.fullmatch(rf"SELECT\s+[^;]+?\s+FROM\s+`?{table}`?\s+WHERE\s+(.+?)\s+FOR\s+UPDATE", holder, re.I)
    w = re.fullmatch(rf"UPDATE\s+`?{table}`?\s+SET\s+.+?\s+WHERE\s+(.+)", waiter, re.I)
    predicate = intent["target"]["predicate"].strip()
    if not h or not w or h[1].strip() != predicate or w[1].strip() != predicate:
        raise IntentCompilationError("Holder and waiter must use the target table and identical target predicate")
    if re.search(r"\b(SELECT|JOIN|UNION)\b", predicate, re.I):
        raise IntentCompilationError("Hot-row predicates cannot contain subqueries")
    return holder, waiter


def _compile(payload: Any, ctx: CompilationContext) -> CompilationResult:
    plan = validate_intent_plan(payload, ctx.request.injected_nodes)
    if not ctx.run_id or not ctx.compilation_id or type(ctx.round_no) is not int or ctx.round_no < 1:
        raise IntentCompilationError("Explicit run, round and compilation identity required")
    database = ctx.request.target_database
    _identifier(database)
    limit = min(_positive(ctx.request.max_duration_sec, "request duration"),
                _positive(ctx.config.max_duration_sec, "config duration"))
    workload = ctx.request.workload or {}
    if workload.get("enabled"):
        limit = min(limit, _positive(workload.get("injection_observe_sec"), "observation window"))
    result = CompilationResult()
    for index, intent in enumerate(plan["intents"]):
        node = intent["node"]
        strategy = require_compilable_strategy(node, intent["strategy"])
        if node not in COMPILED_NODES:
            raise IntentCompilationError(f"No compiler for {node}")
        identity = "\0".join((ctx.run_id, str(ctx.round_no), ctx.compilation_id, node))
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:24]
        task_id = f"{node}_{suffix}"
        source = f"intents[{index}]"
        load = intent["load"]
        duration = min(load["duration_sec"], limit)
        action: dict[str, Any] = {"database": database, "duration_sec": duration}
        if duration != load["duration_sec"]:
            result.normalizations.append({"source": source + ".load.duration_sec", "reason": "duration_limit",
                                          "original": load["duration_sec"], "final": duration})
        concurrency = load.get("concurrency")
        if concurrency is None and "load.concurrency" in strategy.defaults:
            concurrency = strategy.defaults["load.concurrency"]
            result.normalizations.append({"source": source + ".load.concurrency", "reason": "strategy_default",
                                          "original": None, "final": concurrency})
        base = f"task_specs[{index}].actions[0]"
        if node in {"missing_index", "improper_sql", "large_temp_table"}:
            sql = _single_sql(intent["sql"]["query"])
            if not re.match(r"(?:SELECT|WITH)\b", sql, re.I):
                raise IntentCompilationError("Query strategy requires SELECT")
            action.update(kind="raw_sql_workload", sql=sql, concurrency=concurrency)
            result.source_map[base + ".sql"] = source + ".sql.query"
        elif node in {"hot_update", "long_tx", "table_lock", "metadata_lock"}:
            table = _identifier(intent["target"]["table"])
            hold = min(load["hold_sec"], duration)
            if hold != load["hold_sec"]:
                result.normalizations.append({"source": source + ".load.hold_sec", "reason": "duration_limit",
                                              "original": load["hold_sec"], "final": hold})
            if node in {"hot_update", "long_tx"}:
                holder, waiter = _hot_sql(intent)
                holder_steps = [{"sql": "BEGIN"}, {"sql": holder}, {"sleep_sec": hold}, {"sql": "ROLLBACK"}]
                waiter_steps = [{"sql": "BEGIN"}, {"sql": waiter}, {"sql": "ROLLBACK"}]
                result.source_map[base + ".scripts[0].steps[1].sql"] = source + ".sql.holder_sql"
                result.source_map[base + ".scripts[1].steps[1].sql"] = source + ".sql.waiter_sql"
            elif node == "table_lock":
                waiter = _single_sql(intent["sql"]["waiter_sql"])
                if not re.fullmatch(rf"SELECT\s+.+?\s+FROM\s+`?{re.escape(intent['target']['table'])}`?(?:\s+WHERE\s+.+)?", waiter, re.I):
                    raise IntentCompilationError("Table-lock waiter must read the target table")
                holder_steps = [{"sql": f"LOCK TABLES {table} WRITE"}, {"sleep_sec": hold}, {"sql": "UNLOCK TABLES"}]
                waiter_steps = [{"sql": waiter}]
                result.source_map[base + ".scripts[1].steps[0].sql"] = source + ".sql.waiter_sql"
            else:
                holder = _single_sql(intent["sql"]["holder_sql"])
                waiter = _single_sql(intent["sql"]["waiter_sql"])
                name = re.escape(intent["target"]["table"])
                if not re.fullmatch(rf"SELECT\s+.+?\s+FROM\s+`?{name}`?(?:\s+WHERE\s+.+)?", holder, re.I):
                    raise IntentCompilationError("MDL holder must read the target table")
                # FORCE preserves the logical schema, unlike arbitrary ALTER clauses.
                if not re.fullmatch(rf"ALTER\s+TABLE\s+`?{name}`?\s+FORCE", waiter, re.I):
                    raise IntentCompilationError("MDL compiler only supports schema-preserving ALTER TABLE target FORCE")
                holder_steps = [{"sql": "BEGIN"}, {"sql": holder}, {"sleep_sec": hold}, {"sql": "ROLLBACK"}]
                waiter_steps = [{"sql": waiter}]
                result.source_map[base + ".scripts[0].steps[1].sql"] = source + ".sql.holder_sql"
                result.source_map[base + ".scripts[1].steps[0].sql"] = source + ".sql.waiter_sql"
                result.warnings.append(f"{task_id}: ALTER FORCE rebuilds the table; DDL runtime/cancellation must be validated")
            action.update(kind="raw_transaction_script", concurrency=concurrency, scripts=[
                {"role": "holder", "concurrency": 1, "steps": holder_steps},
                {"role": "waiter", "concurrency": concurrency - 1, "steps": waiter_steps},
            ])
            result.warnings.append(f"{task_id}: executor has no holder readiness barrier; verify ordering and target row existence")
        elif node == "traffic_surge":
            if not workload.get("enabled") or not ctx.benchbase_profile or not ctx.transaction_types:
                raise IntentCompilationError("Traffic surge requires an active profile and explicit transaction types")
            profile = copy.deepcopy(ctx.benchbase_profile)
            if profile.get("database") != database or profile.get("benchmark") != workload.get("benchmark"):
                raise IntentCompilationError("BenchBase profile does not match request workload")
            profile.update(terminals=concurrency, duration_sec=duration)
            if load["rate"] is not None:
                profile["rate"] = load["rate"]
            profile = validate_traffic_surge_profile(profile, list(ctx.transaction_types))
            action.update(kind="benchbase_burst", profile=profile,
                          jar_path=ctx.config.benchbase_jar_path, java_bin=ctx.config.benchbase_java_bin)
        elif node == "backup":
            table = intent["target"]["table"]
            _identifier(table)
            command = [_absolute(ctx.mysqldump_path, "mysqldump_path"),
                       "--defaults-extra-file=" + _absolute(ctx.mysql_defaults_file, "mysql_defaults_file"),
                       "--host=" + ctx.config.mysql_host, "--port=" + str(ctx.config.mysql_port),
                       "--user=" + ctx.config.mysql_user, "--single-transaction", "--skip-lock-tables"]
            predicate = intent["target"]["predicate"]
            if predicate is not None:
                command.append("--where=" + _single_sql(predicate))
            command.extend([database, table])
            action.update(kind="logical_backup_command", command=command)
            result.warnings.append(f"{task_id}: dump retained in executor stdout artifact; logical backup creates IO, not explicit table locks")
        else:
            values = ctx.resource_profiles.get(node, {}).get(load["intensity"])
            if not values:
                raise IntentCompilationError(f"Explicit resource profile required: {node}/{load['intensity']}")
            blade = _absolute(ctx.config.chaosblade_path, "chaosblade_path")
            if node in {"resource_cpu", "resource_memory"}:
                percent = _positive(values.get("percent"), "resource percent")
                if percent > (min(100, ctx.config.max_cpu_usage) if node == "resource_cpu" else 100):
                    raise IntentCompilationError("Resource percent exceeds limit")
                target, flag = ("cpu", "--cpu-percent") if node == "resource_cpu" else ("mem", "--mem-percent")
                command = [blade, "create", target, flag, str(percent)]
            elif node in {"resource_io", "disk_full_or_pressure"}:
                size = values.get("size")
                if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*[MK]", size):
                    raise IntentCompilationError("Disk profile requires a bounded size in K or M")
                command = [blade, "create", "disk", "--path", _absolute(values.get("path", ""), "disk path"), "--size", size]
            else:
                raise IntentCompilationError(f"No compiler for {node}")
            action.update(kind="raw_command", command=command, cleanup_command=[blade, "destroy", suffix])
            canonicalize_resource_chaosblade_action(ctx.config, action, root_cause=node, assigned_uid=suffix)
            result.normalizations.append({"source": source + ".load.intensity", "reason": "resource_profile",
                                          "original": load["intensity"], "final": copy.deepcopy(values)})
        risk = "high" if node.startswith("resource_") or node in {"disk_full_or_pressure", "table_lock", "metadata_lock"} else "medium"
        result.task_specs.append(TaskSpec(
            task_id=task_id, task_type=node, actions=[action], risk_assessment=risk,
            metadata={"root_cause": node, "strategy": intent["strategy"], "intent_version": INTENT_VERSION,
                      "compiler_version": COMPILER_VERSION, "expected_evidence": intent["expected_evidence"],
                      "cleanup_owner": "executor: command finally or transaction connection close"},
        ))
        result.source_map[f"task_specs[{index}]"] = source
        result.warnings.extend(f"{task_id}: {r}" for r in strategy.requirements)
    return result


def compile_intent_plan(payload: Any, context: CompilationContext) -> CompilationResult:
    """Construct tasks only. Caller must run SQL, DAG, timing and safety validation."""
    try:
        return _compile(payload, context)
    except IntentCompilationError:
        raise
    except (IntentValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise IntentCompilationError(str(exc)) from exc
