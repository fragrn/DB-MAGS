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
import time
import uuid
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
    "build_traffic_task",
    "build_lock_task",
    "build_chaos_task",
    "build_backup_task",
    "build_task_dag",
    "check_safety",
    "read_memory",
}


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
    database: str,
    task_id: str = "",
    root_cause: str = "traffic_surge",
    target_connections: int = 50,
    ramp_stages: Optional[List[dict]] = None,
    duration_sec: int = 60,
) -> dict[str, Any]:
    """
    Build a TaskSpec for traffic surge / connection ramp.

    ramp_stages: list of {at_sec, connections} defining how many
    concurrent sessions to add at each stage.
    """
    if not task_id:
        task_id = f"traffic_{uuid.uuid4().hex[:6]}"

    if ramp_stages is None:
        ramp_stages = [
            {"at_sec": 0, "connections": max(target_connections // 3, 10)},
            {"at_sec": 20, "connections": max(target_connections * 2 // 3, 20)},
            {"at_sec": 40, "connections": target_connections},
        ]

    actions = [
        {
            "kind": "workload_ramp",
            "database": database,
            "ramp_stages": ramp_stages,
            "duration_sec": duration_sec,
            "sql": "SELECT 1",
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
            "root_cause": root_cause,
            "target_connections": target_connections,
            "ramp_stages": ramp_stages,
        },
    }


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
) -> dict[str, Any]:
    """
    Run safety checks against an ExecutableTaskDAG.

    Returns {approved: bool, reasons: [...], warnings: [...]}.
    """
    from agent.safety import SafetyChecker

    checker = SafetyChecker(config)
    result = checker.check(task_dag, current_db_metrics, current_os_metrics)
    return {
        "approved": result.approved,
        "reasons": result.reasons,
        "warnings": result.warnings,
    }


@tool("execute_dag")
def execute_dag(task_dag: dict, config: RuntimeConfig, max_duration_sec: int = 300) -> dict[str, Any]:
    """
    Execute an ExecutableTaskDAG and return an ExecutionTrace.
    """
    from agent.executor import Executor

    executor = Executor(config)
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        result: dict[str, Any] = {"text": text}
        if json_mode:
            try:
                result["json_payload"] = json.loads(text)
            except json.JSONDecodeError:
                pass
        return result
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
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
