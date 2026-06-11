"""
ReAct tool registry for the global planner.

Each tool is a plain function (not an Agent) that the LLM calls via the
ReAct loop.  Tools are grouped into three categories:

  1. Environment probe tools   — read-only introspection
  2. TaskSpec builder tools     — replace the former SpecialistAgents
  3. Orchestration tools        — DAG, safety, memory, execute
"""

from __future__ import annotations

import inspect
import json
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional

from agent.config import RuntimeConfig
from agent.types import to_jsonable
from agent.probes.mysql_probe import MySQLProbe
from agent.probes.os_probe import OSProbe


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, callable] = {}

PLANNING_TOOL_NAMES = {
    "probe_full_snapshot",
    "probe_schema",
    "probe_table_stats",
    "probe_db_metrics",
    "probe_os_metrics",
    "explain_sql",
    "latency_sample",
    "build_slow_sql_task",
    "get_benchbase_workload_defaults",
    "build_traffic_task",
    "build_lock_task",
    "build_chaos_task",
    "build_backup_task",
    "build_task_dag",
    "check_safety",
    "read_memory",
}

BENCHBASE_BENCHMARKS = {
    "tpcc": {
        "transaction_types": ["NewOrder", "Payment", "OrderStatus", "Delivery", "StockLevel"],
        "default_mix": {"NewOrder": 45, "Payment": 43, "OrderStatus": 4, "Delivery": 4, "StockLevel": 4},
    },
    "tpch": {
        "transaction_types": [f"Q{i}" for i in range(1, 23)],
        "default_mix": {f"Q{i}": 1 for i in range(1, 23)},
    },
    "tatp": {
        "transaction_types": [
            "DeleteCallForwarding",
            "GetAccessData",
            "GetNewDestination",
            "GetSubscriberData",
            "InsertCallForwarding",
            "UpdateLocation",
            "UpdateSubscriberData",
        ],
        "default_mix": {
            "DeleteCallForwarding": 2,
            "GetAccessData": 35,
            "GetNewDestination": 10,
            "GetSubscriberData": 35,
            "InsertCallForwarding": 2,
            "UpdateLocation": 14,
            "UpdateSubscriberData": 2,
        },
    },
}
TPCC_TRANSACTION_TYPES = BENCHBASE_BENCHMARKS["tpcc"]["transaction_types"]
TRAFFIC_SURGE_PROFILE_FIELDS = {
    "benchmark",
    "database",
    "config_path",
    "terminals",
    "rate",
    "duration_sec",
    "transaction_mix",
    "mix_template",
    "rationale",
}
TRAFFIC_SURGE_FORBIDDEN_FIELDS = {
    "sql",
    "query",
    "queries",
    "lock_holder",
    "lock_sql",
    "lock_task",
    "slow_sql",
    "custom_actions",
    "extra_tasks",
    "actions",
    "ramp_stages",
    "target_connections",
}


class LLMTimeoutError(TimeoutError):
    """Raised when an OpenAI-compatible chat completion request times out."""


LLM_HTTP_TIMEOUT_SEC = 120
LLM_HTTP_MAX_ATTEMPTS = 2


def _read_chat_completion_json(req: Any, *, timeout_sec: int = LLM_HTTP_TIMEOUT_SEC, label: str) -> dict[str, Any]:
    """Read an OpenAI-compatible chat completion response with one retry on timeout."""
    import urllib.request

    last_timeout: BaseException | None = None
    for attempt in range(1, LLM_HTTP_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except (TimeoutError, socket.timeout) as exc:
            last_timeout = exc
            if attempt < LLM_HTTP_MAX_ATTEMPTS:
                time.sleep(1.0)
                continue
    raise LLMTimeoutError(
        f"{label} timed out after {timeout_sec}s "
        f"(attempts={LLM_HTTP_MAX_ATTEMPTS}): {last_timeout}"
    )


def tool(name: str):
    """Decorator that registers a function as a callable ReAct tool."""
    def decorator(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Probe tools
# ---------------------------------------------------------------------------


@tool("probe_schema")
def probe_schema(config: RuntimeConfig, database: str) -> dict[str, Any]:
    """
    Probe database schema: table names, columns (name/type/key/nullable), indexes.

    Returns a dict mapping table_name -> {columns: [...], indexes: [...]}.
    Must be called before generating any TaskSpec.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    schema = probe.schema()
    indexes = probe.indexes()
    for tname in schema:
        schema[tname]["indexes"] = indexes.get(tname, [])
    return schema


@tool("probe_table_stats")
def probe_table_stats(config: RuntimeConfig, database: str) -> list[dict[str, Any]]:
    """
    Probe table sizes and row counts, ordered largest first.

    Returns a list of dicts with TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    return probe.table_stats()


@tool("probe_db_metrics")
def probe_db_metrics(config: RuntimeConfig, database: str) -> dict[str, Any]:
    """
    Probe current MySQL global status, variables, and processlist.

    Returns a flat dict with selected STATUS variables, VARIABLES, and a
    processlist array.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    return probe.db_metrics()


@tool("probe_workload")
def probe_workload(config: RuntimeConfig, database: str, interval_sec: float = 3.0) -> dict[str, Any]:
    """
    Measure QPS and TPS over `interval_sec` seconds.

    Returns {qps, tps, interval_sec}.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    return probe.workload_probe(interval_sec)


@tool("probe_os_metrics")
def probe_os_metrics() -> dict[str, Any]:
    """
    Collect OS-level metrics: CPU, memory, disk, load average, network, FDs.
    """
    return OSProbe().collect()


@tool("probe_full_snapshot")
def probe_full_snapshot(config: RuntimeConfig, database: str) -> dict[str, Any]:
    """
    One-shot full environment snapshot: schema, table stats, DB metrics,
    workload, OS metrics, and DB version.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    os_probe = OSProbe()
    schema = probe.schema()
    indexes = probe.indexes()
    for tname in schema:
        schema[tname]["indexes"] = indexes.get(tname, [])
    return {
        "database": database,
        "db_version": probe.version(),
        "schema": schema,
        "table_stats": probe.table_stats(),
        "db_metrics": probe.db_metrics(),
        "workload": probe.workload_probe(interval_sec=3.0),
        "os_metrics": os_probe.collect(),
    }


@tool("explain_sql")
def explain_sql(config: RuntimeConfig, database: str, sql: str) -> dict[str, Any]:
    """
    Run EXPLAIN on a SQL statement and return the plan rows.

    Use this to verify whether a candidate SQL will actually produce the
    intended root cause (e.g. type=ALL, Using filesort).
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    plan = probe.explain(sql)
    return {"sql": sql, "plan": plan}


@tool("latency_sample")
def latency_sample(config: RuntimeConfig, database: str, sql: str, n: int = 5) -> dict[str, Any]:
    """
    Run a SQL statement `n` times and return latency statistics (min/p50/p95/max ms).
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    return probe.latency_sample(sql, n=n)


# ---------------------------------------------------------------------------
# TaskSpec builder tools  (replace SpecialistAgents)
# ---------------------------------------------------------------------------


@tool("build_slow_sql_task")
def build_slow_sql_task(
    config: RuntimeConfig,
    database: str,
    task_id: str = "",
    root_cause: str = "missing_index",
    table: str = "",
    column: str = "",
    predicate: str = "",
    sort_column: str = "",
    pattern: str = "range_scan",
    limit: int = 100,
    concurrency: int = 8,
    duration_sec: int = 30,
) -> dict[str, Any]:
    """
    Build a TaskSpec for a slow-SQL anomaly.

    Parameters
    ----------
    pattern: one of "range_scan", "sort_filesort", "large_scan",
             "weak_predicate", "function_on_column", "multi_join"
    predicate: the WHERE clause predicate string
    sort_column: column to ORDER BY (used for filesort pattern)
    concurrency: number of parallel threads executing the SQL
    duration_sec: how long to run the workload
    """
    if not task_id:
        task_id = f"slow_sql_{uuid.uuid4().hex[:6]}"

    # Determine SQL based on pattern
    if pattern == "range_scan":
        sql = f"SELECT * FROM {table} WHERE {predicate} LIMIT {limit}"
    elif pattern == "sort_filesort":
        sql = f"SELECT * FROM {table} ORDER BY {sort_column} DESC LIMIT {limit}"
    elif pattern == "large_scan":
        sql = f"SELECT * FROM {table} WHERE {predicate} LIMIT {limit}"
    elif pattern == "weak_predicate":
        sql = f"SELECT * FROM {table} WHERE {predicate} LIMIT {limit}"
    elif pattern == "function_on_column":
        sql = f"SELECT * FROM {table} WHERE {predicate} LIMIT {limit}"
    elif pattern == "multi_join":
        sql = f"SELECT * FROM {table} t1 JOIN {table} t2 ON t1.{column}=t2.{column} LIMIT {limit}"
    else:
        sql = f"SELECT * FROM {table} LIMIT {limit}"

    actions = [
        {
            "kind": "sql_workload",
            "sql": sql,
            "concurrency": concurrency,
            "duration_sec": duration_sec,
            "database": database,
        }
    ]

    return {
        "task_id": task_id,
        "task_type": "slow_sql",
        "actions": actions,
        "expected_metrics": {
            "rows_examined_delta": ">0",
            "slow_query_delta": ">=1",
            "p95_latency_ratio": ">=1.5",
        },
        "success_criteria": {
            "slow_query_delta": ">=1",
        },
        "risk_assessment": "low",
        "metadata": {
            "root_cause": root_cause,
            "pattern": pattern,
            "table": table,
            "sql": sql,
        },
    }


@tool("build_traffic_task")
def build_traffic_task(
    config: RuntimeConfig,
    profile: dict[str, Any],
    task_id: str = "",
) -> dict[str, Any]:
    """
    Build a traffic_surge TaskSpec from a validated BenchBase burst profile.
    """
    normalized_profile = validate_traffic_surge_profile(profile)
    if not task_id:
        task_id = f"traffic_{uuid.uuid4().hex[:6]}"

    actions = [
        {
            "kind": "benchbase_burst",
            "profile": dict(normalized_profile),
            "benchmark": normalized_profile["benchmark"],
            "database": normalized_profile["database"],
            "config_path": normalized_profile["config_path"],
            "terminals": normalized_profile["terminals"],
            "rate": normalized_profile["rate"],
            "duration_sec": normalized_profile["duration_sec"],
            "transaction_mix": dict(normalized_profile["transaction_mix"]),
        }
    ]

    return {
        "task_id": task_id,
        "task_type": "traffic_surge",
        "actions": actions,
        "expected_metrics": {
            "threads_connected_ratio": ">=1.5",
            "threads_running_ratio": ">=1.5",
        },
        "success_criteria": {
            "threads_connected_ratio": ">=1.5",
        },
        "risk_assessment": "medium",
        "metadata": {
            "root_cause": "traffic_surge",
            "traffic_surge_profile": dict(normalized_profile),
        },
    }


@tool("get_benchbase_workload_defaults")
def get_benchbase_workload_defaults(
    benchmark: str,
    config_path: str = "",
    database: str = "",
    terminals: int = 0,
    rate: str = "",
    duration_sec: float = 0,
) -> dict[str, Any]:
    """
    Return the current BenchBase workload defaults and constraints, with no candidate templates.
    """
    benchmark = _normalize_benchmark(benchmark)
    xml_defaults = benchbase_workload_defaults(benchmark, config_path)
    resolved_terminals = int(terminals) if terminals else xml_defaults.get("terminals")
    resolved_rate = rate if rate not in ("", None) else xml_defaults.get("rate")
    resolved_duration = float(duration_sec) if duration_sec else xml_defaults.get("duration_sec")
    return {
        "benchmark": benchmark,
        "database": database,
        "config_path": config_path,
        "legal_transaction_types": list(xml_defaults["transaction_types"]),
        "default_transaction_mix": dict(xml_defaults["transaction_mix"]),
        "default_terminals": resolved_terminals,
        "default_rate": resolved_rate,
        "default_duration_sec": resolved_duration,
        "constraints": {
            "profile_fields": sorted(TRAFFIC_SURGE_PROFILE_FIELDS),
            "forbidden_fields": sorted(TRAFFIC_SURGE_FORBIDDEN_FIELDS),
            "transaction_mix": f"dict of {benchmark} transaction type to non-negative numeric weight; sum must be > 0",
            "terminals": "positive integer; safety checker enforces connection headroom",
            "rate": "positive numeric requests/second target or 'unlimited'",
            "duration_sec": "positive numeric seconds; runtime requires it to fit request max_duration_sec and injection_observe_sec",
        },
        "policy": (
            "Initial round should use these defaults. After reflection, the LLM may adjust "
            "terminals, rate, duration_sec, and transaction_mix based on evaluation feedback."
        ),
    }


def validate_traffic_surge_profile(profile: dict[str, Any], transaction_types: list[str] | None = None) -> dict[str, Any]:
    """Validate and normalize a TrafficSurgeProfile for a BenchBase burst."""
    if not isinstance(profile, dict):
        raise ValueError("TrafficSurgeProfile must be an object")
    forbidden = sorted(k for k in profile if k in TRAFFIC_SURGE_FORBIDDEN_FIELDS)
    if forbidden:
        raise ValueError(f"TrafficSurgeProfile contains forbidden fields: {', '.join(forbidden)}")
    extra = sorted(k for k in profile if k not in TRAFFIC_SURGE_PROFILE_FIELDS)
    if extra:
        raise ValueError(f"TrafficSurgeProfile contains unknown fields: {', '.join(extra)}")
    missing = sorted(k for k in TRAFFIC_SURGE_PROFILE_FIELDS if k not in profile)
    if missing:
        raise ValueError(f"TrafficSurgeProfile missing required fields: {', '.join(missing)}")
    benchmark = _normalize_benchmark(profile["benchmark"])
    database = str(profile["database"]).strip()
    config_path = str(profile["config_path"]).strip()
    mix_template = str(profile["mix_template"]).strip()
    rationale = str(profile["rationale"]).strip()
    if not database:
        raise ValueError("TrafficSurgeProfile database is required")
    if not config_path:
        raise ValueError("TrafficSurgeProfile config_path is required")
    if not mix_template:
        raise ValueError("TrafficSurgeProfile mix_template is required")
    if not rationale:
        raise ValueError("TrafficSurgeProfile rationale is required")
    terminals = int(profile["terminals"])
    rate = _normalize_rate(profile["rate"])
    duration_sec = float(profile["duration_sec"])
    if terminals <= 0:
        raise ValueError("TrafficSurgeProfile terminals must be > 0")
    if rate != "unlimited" and float(rate) <= 0:
        raise ValueError("TrafficSurgeProfile rate must be > 0")
    if duration_sec <= 0:
        raise ValueError("TrafficSurgeProfile duration_sec must be > 0")
    raw_mix = profile["transaction_mix"]
    if not isinstance(raw_mix, dict):
        raise ValueError("TrafficSurgeProfile transaction_mix must be an object")
    ordered_types = list(transaction_types or benchbase_transaction_types(benchmark, config_path))
    unknown = sorted(k for k in raw_mix if k not in ordered_types)
    if unknown:
        raise ValueError(f"TrafficSurgeProfile transaction_mix has unknown transaction types: {', '.join(unknown)}")
    mix: dict[str, float] = {}
    for name in ordered_types:
        value = float(raw_mix.get(name, 0))
        if value < 0:
            raise ValueError(f"TrafficSurgeProfile transaction_mix weight for {name} must be >= 0")
        mix[name] = value
    if sum(mix.values()) <= 0:
        raise ValueError("TrafficSurgeProfile transaction_mix total weight must be > 0")
    return {
        "benchmark": benchmark,
        "database": database,
        "config_path": config_path,
        "terminals": terminals,
        "rate": rate,
        "duration_sec": duration_sec,
        "transaction_mix": mix,
        "mix_template": mix_template,
        "rationale": rationale,
    }


def benchbase_transaction_types(benchmark: str, config_path: str = "") -> list[str]:
    """Return transaction order for a BenchBase benchmark, preferring the XML config."""
    benchmark = _normalize_benchmark(benchmark)
    if config_path:
        try:
            path = Path(config_path).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            if path.exists():
                root = ET.parse(path).getroot()
                names = [
                    str(name.text).strip()
                    for name in root.findall(".//transactiontypes/transactiontype/name")
                    if name.text and str(name.text).strip()
                ]
                if names:
                    return names
        except Exception:
            pass
    return list(BENCHBASE_BENCHMARKS[benchmark]["transaction_types"])


def benchbase_workload_defaults(benchmark: str, config_path: str = "") -> dict[str, Any]:
    """Return transaction order, weights, rate, time, and terminals from XML or benchmark defaults."""
    benchmark = _normalize_benchmark(benchmark)
    transaction_types = benchbase_transaction_types(benchmark, config_path)
    defaults = {
        "transaction_types": transaction_types,
        "transaction_mix": _align_mix(BENCHBASE_BENCHMARKS[benchmark]["default_mix"], transaction_types),
        "terminals": None,
        "rate": None,
        "duration_sec": None,
    }
    if not config_path:
        return defaults
    try:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            return defaults
        root = ET.parse(path).getroot()
        terminals_elem = root.find("terminals")
        if terminals_elem is not None and terminals_elem.text:
            defaults["terminals"] = int(float(terminals_elem.text.strip()))
        work = root.find(".//works/work")
        if work is None:
            work = root.find(".//work")
        if work is not None:
            rate_elem = work.find("rate")
            time_elem = work.find("time")
            weights_elem = work.find("weights")
            if rate_elem is not None and rate_elem.text:
                raw_rate = rate_elem.text.strip()
                defaults["rate"] = "unlimited" if raw_rate.lower() == "unlimited" else float(raw_rate)
            if time_elem is not None and time_elem.text:
                defaults["duration_sec"] = float(time_elem.text.strip())
            if weights_elem is not None and weights_elem.text:
                weights = [float(x.strip()) for x in weights_elem.text.split(",") if x.strip()]
                if len(weights) == len(transaction_types):
                    defaults["transaction_mix"] = dict(zip(transaction_types, weights))
    except Exception:
        return defaults
    return defaults


def _align_mix(mix: dict[str, Any], transaction_types: list[str]) -> dict[str, float]:
    return {name: float(mix.get(name, 0)) for name in transaction_types}


def _normalize_benchmark(benchmark: Any) -> str:
    value = str(benchmark or "").strip().lower()
    if value not in BENCHBASE_BENCHMARKS:
        raise ValueError(f"Unsupported BenchBase benchmark: {benchmark}")
    return value


def _normalize_rate(rate: Any) -> float | str:
    if isinstance(rate, str) and rate.strip().lower() == "unlimited":
        return "unlimited"
    return float(rate)




@tool("build_lock_task")
def build_lock_task(
    config: RuntimeConfig,
    database: str,
    task_id: str = "",
    root_cause: str = "hot_update",
    table: str = "",
    key_column: str = "",
    holder_concurrency: int = 2,
    waiter_concurrency: int = 8,
    hold_sec: float = 30.0,
    lock_type: str = "record_lock",
) -> dict[str, Any]:
    """
    Build a TaskSpec for lock contention / hot-row conflict.

    lock_type: "record_lock" | "table_lock" | "metadata_lock"
    """
    if not task_id:
        task_id = f"lock_{uuid.uuid4().hex[:6]}"

    actions = [
        {
            "kind": "lock_conflict",
            "database": database,
            "table": table,
            "key_column": key_column,
            "holder_concurrency": holder_concurrency,
            "waiter_concurrency": waiter_concurrency,
            "hold_sec": hold_sec,
            "lock_type": lock_type,
        }
    ]

    cleanup = [
        {
            "kind": "kill_blocking_sessions",
            "database": database,
        }
    ]

    return {
        "task_id": task_id,
        "task_type": "lock_conflict",
        "actions": actions,
        "cleanup_actions": cleanup,
        "expected_metrics": {
            "lock_waits_ratio": ">=2.0",
            "blocked_session_count": ">=1",
        },
        "success_criteria": {
            "blocked_session_count": ">=1",
        },
        "risk_assessment": "medium",
        "metadata": {
            "root_cause": root_cause,
            "table": table,
            "key_column": key_column,
            "lock_type": lock_type,
        },
    }


@tool("build_chaos_task")
def build_chaos_task(
    config: RuntimeConfig,
    task_id: str = "",
    root_cause: str = "resource_cpu",
    resource_type: str = "cpu",
    duration_sec: int = 30,
    intensity: str = "high",
    chaosblade_path: str = "",
) -> dict[str, Any]:
    """
    Build a TaskSpec for an OS-level resource bottleneck via ChaosBlade.
    """
    if not task_id:
        task_id = f"chaos_{uuid.uuid4().hex[:6]}"

    if not chaosblade_path:
        chaosblade_path = config.chaosblade_path

    actions = [
        {
            "kind": "chaosblade",
            "resource_type": resource_type,
            "duration_sec": duration_sec,
            "intensity": intensity,
            "chaosblade_path": chaosblade_path,
        }
    ]

    cleanup = [
        {
            "kind": "chaosblade_destroy",
            "uid": f"{{{task_id}_uid}}",  # placeholder; filled at execution time
        }
    ]

    risk = "high" if resource_type in ("cpu", "io") else "medium"

    return {
        "task_id": task_id,
        "task_type": "chaos",
        "actions": actions,
        "cleanup_actions": cleanup,
        "expected_metrics": {
            f"{resource_type}_usage_ratio": ">=1.5",
        },
        "success_criteria": {},
        "risk_assessment": risk,
        "metadata": {
            "root_cause": root_cause,
            "resource_type": resource_type,
            "duration_sec": duration_sec,
            "intensity": intensity,
        },
    }


@tool("build_backup_task")
def build_backup_task(
    config: RuntimeConfig,
    database: str,
    task_id: str = "",
    root_cause: str = "backup",
    table: str = "",
    tool: str = "mysqldump",
) -> dict[str, Any]:
    """
    Build a TaskSpec for a logical backup / maintenance operation.
    """
    if not task_id:
        task_id = f"backup_{uuid.uuid4().hex[:6]}"

    actions = [
        {
            "kind": "logical_backup",
            "database": database,
            "table": table,
            "tool": tool,
        }
    ]

    cleanup = [
        {
            "kind": "terminate_backup",
            "pid_file": f"/tmp/{task_id}.pid",
        }
    ]

    return {
        "task_id": task_id,
        "task_type": "backup",
        "actions": actions,
        "cleanup_actions": cleanup,
        "expected_metrics": {
            "io_wait_ratio": ">=1.5",
        },
        "success_criteria": {
            "io_wait_ratio": ">=1.3",
        },
        "risk_assessment": "medium",
        "metadata": {
            "root_cause": root_cause,
            "table": table,
            "tool": tool,
        },
    }


# ---------------------------------------------------------------------------
# Orchestration tools
# ---------------------------------------------------------------------------


@tool("build_task_dag")
def build_task_dag(task_specs: list[dict], dependencies: Optional[List[List[str]]] = None) -> dict[str, Any]:
    """
    Build an ExecutableTaskDAG from a list of TaskSpec dicts.

    dependencies: list of [source_task_id, target_task_id] pairs.
    """
    from agent.dag import build_task_dag as _build

    return _build(task_specs, dependencies or [])


@tool("check_safety")
def check_safety(
    task_dag: dict,
    config: RuntimeConfig,
    current_db_metrics: dict | None = None,
    current_os_metrics: dict | None = None,
    max_duration_sec: float | None = None,
    injection_observe_sec: float | None = None,
    expected_workload: dict | None = None,
) -> dict[str, Any]:
    """
    Run safety checks against an ExecutableTaskDAG.

    Returns {approved: bool, reasons: [...], warnings: [...]}.
    """
    from agent.safety import SafetyChecker

    checker = SafetyChecker(config)
    result = checker.check(
        task_dag,
        current_db_metrics,
        current_os_metrics,
        max_duration_sec=max_duration_sec,
        injection_observe_sec=injection_observe_sec,
        expected_workload=expected_workload,
    )
    return {
        "approved": result.approved,
        "reasons": result.reasons,
        "warnings": result.warnings,
    }


@tool("execute_dag")
def execute_dag(
    task_dag: dict,
    config: RuntimeConfig,
    max_duration_sec: int = 300,
    round_dir: str = "",
) -> dict[str, Any]:
    """
    Execute an ExecutableTaskDAG and return an ExecutionTrace.
    """
    from agent.executor import Executor

    executor = Executor(config, round_dir=round_dir or None)
    trace = executor.execute(task_dag, max_duration_sec=max_duration_sec)
    return to_jsonable(trace)


@tool("collect_baseline_metrics")
def collect_baseline_metrics(config: RuntimeConfig, database: str) -> dict[str, Any]:
    """
    Collect a baseline metrics snapshot before experiment execution.
    """
    probe = MySQLProbe(
        database=database,
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
    )
    os_probe = OSProbe()
    db_metrics = probe.db_metrics()
    workload = probe.workload_probe(interval_sec=3.0)
    os_metrics = os_probe.collect()
    return {
        "db_metrics": db_metrics,
        "workload": workload,
        "os_metrics": os_metrics,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@tool("read_memory")
def read_memory(
    config: RuntimeConfig,
    anomaly: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Read recent memory items for a given anomaly type.

    Returns a list of MemoryItem dicts, newest first.
    """
    from agent.memory import MemoryStore

    store = MemoryStore(config.memory_file)
    return store.load(anomaly=anomaly, limit=limit)


@tool("write_memory")
def write_memory(config: RuntimeConfig, item: dict) -> dict[str, Any]:
    """
    Append a single MemoryItem to the long-term store.
    """
    from agent.memory import MemoryStore

    store = MemoryStore(config.memory_file)
    store.append(item)
    return {"ok": True, "item": item}


# ---------------------------------------------------------------------------
# LLM client (lightweight wrapper)
# ---------------------------------------------------------------------------

@tool("llm_generate")
def llm_generate(
    config: RuntimeConfig,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    json_mode: bool = True,
) -> dict[str, Any]:
    """
    Call the LLM (OpenAI-compatible) with a system and user prompt.

    Returns {"text": ..., "error": ...} or {"text": ..., "json_payload": ...}.
    """
    try:
        import urllib.request
        import urllib.error
        import json

        url = f"{config.openai_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.openai_api_key}",
        }
        payload: dict[str, Any] = {
            "model": config.openai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if json_mode and not config.openai_model.startswith("gpt-5"):
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        data = _read_chat_completion_json(req, label="LLM request")
        text = data["choices"][0]["message"]["content"]
        result: dict[str, Any] = {"text": text}
        if json_mode:
            try:
                result["json_payload"] = json.loads(text)
            except json.JSONDecodeError:
                pass
        return result
    except LLMTimeoutError:
        raise
    except Exception as e:
        return {"text": "", "error": str(e)}


def planning_tool_schemas() -> list[dict[str, Any]]:
    """Return OpenAI-compatible schemas for tools allowed during planning."""
    return [_tool_schema(name, TOOL_REGISTRY[name]) for name in sorted(PLANNING_TOOL_NAMES)]


def call_planning_tool(name: str, config: RuntimeConfig, arguments: dict[str, Any]) -> Any:
    """Call a planning tool with RuntimeConfig injected locally."""
    if name not in PLANNING_TOOL_NAMES:
        raise ValueError(f"Tool '{name}' is not allowed during planning")
    fn = TOOL_REGISTRY[name]
    kwargs = dict(arguments or {})
    if "config" in inspect.signature(fn).parameters:
        kwargs["config"] = config
    return fn(**kwargs)


def chat_tool_calling_loop(
    config: RuntimeConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    max_steps: int = 12,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """
    Run an OpenAI-compatible chat/completions tool-calling loop.

    The returned dict contains either {"json_payload": ..., "trace": ...} or
    {"error": ..., "trace": ...}. Tool execution is local and limited to
    PLANNING_TOOL_NAMES.
    """
    if not config.openai_api_key or not config.planner_enabled:
        return {"error": "planner disabled or missing OPENAI_API_KEY", "trace": []}

    import urllib.request

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[dict[str, Any]] = []
    tools = planning_tool_schemas()

    for step in range(1, max_steps + 1):
        payload: dict[str, Any] = {
            "model": config.openai_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if not config.openai_model.startswith("gpt-5"):
            payload["temperature"] = temperature
        req = urllib.request.Request(
            f"{config.openai_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.openai_api_key}",
            },
            method="POST",
        )
        try:
            raw = _read_chat_completion_json(req, label=f"LLM tool-calling request at step {step}")
        except LLMTimeoutError as exc:
            raise LLMTimeoutError(
                f"LLM tool-calling request timed out after {LLM_HTTP_TIMEOUT_SEC}s at step {step}: {exc}"
            ) from exc
        except Exception as exc:
            return {"error": str(exc), "trace": trace}

        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            for call in tool_calls:
                fn = (call.get("function") or {})
                name = str(fn.get("name", ""))
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    result = call_planning_tool(name, config=config, arguments=args)
                    content = json.dumps(_truncate_for_prompt(result), ensure_ascii=False)
                    trace.append({"step": step, "tool": name, "arguments": args, "result": _truncate_for_prompt(result)})
                except Exception as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    trace.append({"step": step, "tool": name, "arguments": raw_args, "error": str(exc)})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})
            continue

        text = str(message.get("content") or "")
        trace.append({"step": step, "tool": "final_answer", "content": text[:2000]})
        try:
            return {"json_payload": json.loads(text), "text": text, "trace": trace}
        except json.JSONDecodeError as exc:
            return {"error": f"final JSON parse failed: {exc}", "text": text, "trace": trace}

    return {"error": f"tool calling exceeded max_steps={max_steps}", "trace": trace}


def _tool_schema(name: str, fn: callable) -> dict[str, Any]:
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param_name == "config":
            continue
        properties[param_name] = _json_schema_for_param(param)
        if param.default is inspect._empty:
            required.append(param_name)
    doc = (fn.__doc__ or "").strip().splitlines()
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": doc[0].strip() if doc else name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _json_schema_for_param(param: inspect.Parameter) -> dict[str, Any]:
    annotation = param.annotation
    default = param.default
    schema: dict[str, Any]
    text = str(annotation)
    if annotation is int or "int" in text:
        schema = {"type": "integer"}
    elif annotation is float or "float" in text:
        schema = {"type": "number"}
    elif annotation is bool or "bool" in text:
        schema = {"type": "boolean"}
    elif annotation in (list, list[dict]) or "list" in text:
        schema = {"type": "array"}
    elif annotation is dict or "dict" in text:
        schema = {"type": "object"}
    else:
        schema = {"type": "string"}
    if default is not inspect._empty and default is not None:
        schema["default"] = default
    return schema


def _truncate_for_prompt(value: Any, limit: int = 6000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_tools() -> list[str]:
    """Return all registered tool names."""
    return sorted(TOOL_REGISTRY.keys())


def call_tool(name: str, **kwargs) -> Any:
    """Call a registered tool by name."""
    if name not in TOOL_REGISTRY:
        raise ValueError(f"Unknown tool: {name}")
    return TOOL_REGISTRY[name](**kwargs)
