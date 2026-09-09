"""Local probes, builders, validators, and the OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import re
import copy
import http.client
import socket
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional

from AnomalyGraphAgent.config import RuntimeConfig, resolve_runtime_path
from AnomalyGraphAgent.types import to_jsonable
from AnomalyGraphAgent.probes.mysql_probe import MySQLProbe
from AnomalyGraphAgent.probes.os_probe import OSProbe


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, callable] = {}

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
LLM_HTTP_MAX_ATTEMPTS = 3


def _read_chat_completion_json(req: Any, *, timeout_sec: int = LLM_HTTP_TIMEOUT_SEC, label: str,
                               response_metadata: dict | None = None) -> dict[str, Any]:
    """Read a chat completion response with bounded transport retries."""
    import urllib.error
    import urllib.request

    last_timeout: BaseException | None = None
    last_transport_error: BaseException | None = None
    for attempt in range(1, LLM_HTTP_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read()
                if response_metadata is not None:
                    headers = getattr(resp, "headers", None)
                    if headers is not None:
                        request_id = headers.get("x-request-id")
                        if isinstance(request_id, str):
                            response_metadata["request_id"] = request_id
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except (TimeoutError, socket.timeout) as exc:
            last_timeout = exc
            if attempt < LLM_HTTP_MAX_ATTEMPTS:
                time.sleep(1.0)
                continue
        except urllib.error.HTTPError:
            raise
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ssl.SSLError,
            ConnectionError,
            OSError,
        ) as exc:
            last_transport_error = exc
            if attempt < LLM_HTTP_MAX_ATTEMPTS:
                time.sleep(float(attempt))
                continue
    if last_transport_error is not None:
        raise ConnectionError(
            f"{label} transport failed after {LLM_HTTP_MAX_ATTEMPTS} attempts: "
            f"{last_transport_error}"
        ) from last_transport_error
    raise LLMTimeoutError(
        f"{label} timed out after {timeout_sec}s "
        f"(attempts={LLM_HTTP_MAX_ATTEMPTS}): {last_timeout}"
    )


def tool(name: str):
    """Register a local capability for compatibility with the tool registry."""
    def decorator(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return decorator


def validate_traffic_surge_profile(
    profile: dict[str, Any], transaction_types: list[str] | None = None
) -> dict[str, Any]:
    """Validate and normalize the BenchBase burst profile used by the executor."""
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
    if not database or not config_path or not mix_template or not rationale:
        raise ValueError("TrafficSurgeProfile database, config_path, mix_template and rationale are required")
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
    mix = {}
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
    """Return the transaction order from a BenchBase XML or built-in defaults."""
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
# Orchestration tools
# ---------------------------------------------------------------------------


@tool("build_task_dag")
def build_task_dag(task_specs: list[dict], dependencies: Optional[List[List[str]]] = None) -> dict[str, Any]:
    """
    Build an ExecutableTaskDAG from a list of TaskSpec dicts.

    dependencies: list of [source_task_id, target_task_id] pairs.
    """
    from AnomalyGraphAgent.dag import build_task_dag as _build

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
    from AnomalyGraphAgent.safety import SafetyChecker

    normalized_dag = canonicalize_task_dag_runtime_paths(
        config,
        task_dag,
        expected_workload=expected_workload,
    )
    checker = SafetyChecker(config)
    result = checker.check(
        normalized_dag,
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
        "normalized_task_dag": normalized_dag,
    }


def canonicalize_task_dag_runtime_paths(
    config: RuntimeConfig,
    task_dag: dict[str, Any],
    *,
    expected_workload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace LLM-guessed executable paths with trusted runtime paths."""
    normalized = copy.deepcopy(task_dag)
    workload = expected_workload or {}
    jar_path = resolve_runtime_path(str(workload.get("jar_path") or config.benchbase_jar_path))
    java_bin = str(workload.get("java_bin") or config.benchbase_java_bin)
    if Path(java_bin).is_absolute() or len(Path(java_bin).parts) > 1:
        java_bin = resolve_runtime_path(java_bin)
    config_path = str(workload.get("config_path") or "")
    if config_path:
        config_path = resolve_runtime_path(config_path)

    for task in (normalized.get("tasks") or {}).values():
        if not isinstance(task, dict):
            continue
        root_cause = str((task.get("metadata") or {}).get("root_cause") or "")
        for action in task.get("actions") or []:
            if not isinstance(action, dict):
                continue
            command = action.get("command")
            if action.get("kind") == "benchbase_burst_command" and isinstance(command, list) and command:
                command[0] = java_bin
                _replace_argv_value(command, "-jar", jar_path)
                action_config_path = config_path or _argv_value(command, "-c")
                if action_config_path:
                    _replace_argv_value(command, "-c", resolve_runtime_path(action_config_path))
                action["cwd"] = str(Path(jar_path).parent)
            if root_cause in {
                "resource_cpu",
                "resource_io",
                "resource_memory",
                "resource_network",
                "network_latency",
                "disk_full_or_pressure",
            }:
                if action.get("kind") != "raw_command":
                    continue
                if isinstance(command, list) and command:
                    command[0] = config.chaosblade_path
                cleanup = action.get("cleanup_command")
                if isinstance(cleanup, list) and cleanup:
                    cleanup[0] = config.chaosblade_path
                canonicalize_resource_chaosblade_action(config, action, root_cause=root_cause)
    return normalized


def canonicalize_resource_chaosblade_action(
    config: RuntimeConfig,
    action: dict[str, Any],
    *,
    root_cause: str = "",
    assigned_uid: str | None = None,
) -> dict[str, Any]:
    """Normalize resource ChaosBlade commands to the Darwin CLI shape we run."""
    if action.get("kind") != "raw_command":
        return action
    command = action.get("command")
    if not isinstance(command, list) or not command:
        return action
    command = [str(part) for part in command]
    if len(command) < 3 or command[1] != "create":
        return action
    target = command[2]
    if target not in {"cpu", "disk", "mem", "network"}:
        return action

    original_uid = _argv_value(command, "--uid")
    uid = assigned_uid if assigned_uid is not None else _fresh_chaosblade_uid(root_cause or target)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
        raise ValueError("ChaosBlade uid must be a non-empty safe identifier")
    if original_uid and original_uid != uid:
        action["chaosblade_original_uid"] = original_uid
        action["chaosblade_uid_rewritten"] = True
    duration = _format_chaosblade_timeout(action.get("duration_sec", 30))
    if root_cause == "resource_cpu" or target == "cpu":
        cpu_percent = _argv_value(command, "--cpu-percent") or "90"
        normalized_command = [
            config.chaosblade_path,
            "create",
            "cpu",
            "fullload",
            "--cpu-percent",
            cpu_percent,
            "--timeout",
            duration,
            "--uid",
            uid,
        ]
    elif root_cause == "resource_memory" or target == "mem":
        mem_percent = _argv_value(command, "--mem-percent") or "80"
        mode = _argv_value(command, "--mode") or "ram"
        normalized_command = [
            config.chaosblade_path,
            "create",
            "mem",
            "load",
            "--mode",
            mode,
            "--mem-percent",
            mem_percent,
            "--timeout",
            duration,
            "--uid",
            uid,
        ]
    elif root_cause in {"resource_io", "disk_full_or_pressure"} or target == "disk":
        path = _argv_value(command, "--path") or "/tmp"
        size = _argv_value(command, "--size") or "100M"
        normalized_command = [
            config.chaosblade_path,
            "create",
            "disk",
            "burn",
            "--read",
            "--write",
            "--path",
            path,
            "--size",
            size,
            "--timeout",
            duration,
            "--uid",
            uid,
        ]
    elif root_cause in {"resource_network", "network_latency"} or target == "network":
        port = _argv_value(command, "--destination-port") or _argv_value(command, "--source-port") or "3306"
        traffic = _argv_value(command, "--network-traffic") or "out"
        normalized_command = [
            config.chaosblade_path,
            "create",
            "network",
            "drop",
            "--destination-port",
            port,
            "--network-traffic",
            traffic,
            "--timeout",
            duration,
            "--uid",
            uid,
        ]
    else:
        return action

    action["command"] = normalized_command
    cleanup = action.get("cleanup_command")
    if isinstance(cleanup, list) and cleanup:
        action["cleanup_command"] = [config.chaosblade_path, "destroy", uid]
    return action


def _format_chaosblade_timeout(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 30.0
    if seconds <= 0:
        seconds = 30.0
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:g}"


def _fresh_chaosblade_uid(prefix: str) -> str:
    safe_prefix = "".join(ch if ch.isalnum() else "-" for ch in str(prefix).lower()).strip("-")
    if not safe_prefix:
        safe_prefix = "resource"
    return f"{safe_prefix}-{uuid.uuid4().hex[:12]}"


def _replace_argv_value(command: list[Any], flag: str, value: str) -> None:
    if not value or flag not in command:
        return
    index = command.index(flag) + 1
    if index < len(command):
        command[index] = value


def _argv_value(command: list[Any], flag: str) -> str:
    for index, part in enumerate(command):
        text = str(part)
        if text == flag:
            value_index = index + 1
            if value_index < len(command):
                return str(command[value_index])
            return ""
        if text.startswith(f"{flag}="):
            return text.split("=", 1)[1]
    return ""


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
    from AnomalyGraphAgent.executor import Executor

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
    from AnomalyGraphAgent.memory import MemoryStore

    store = MemoryStore(config.memory_file)
    return store.load(anomaly=anomaly, limit=limit)


@tool("write_memory")
def write_memory(config: RuntimeConfig, item: dict) -> dict[str, Any]:
    """
    Append a single MemoryItem to the long-term store.
    """
    from AnomalyGraphAgent.memory import MemoryStore

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
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Chat Completions; explicit format overrides the legacy JSON-mode policy.

    json_payload is only published for object output. parsed_payload preserves
    other parsed shapes for diagnostics. Returned strings are credential-redacted.
    Local Intent/TaskSpec schema validation remains the caller's responsibility.
    """
    import urllib.error
    import urllib.request

    result: dict[str, Any] = {
        "text": "", "parsed_payload": None, "finish_reason": None, "refusal": None,
        "usage": None, "usage_status": "unavailable", "request_id": None, "response_id": None,
    }

    def finish(error_type: str | None = None, message: str = "") -> dict:
        if error_type:
            result.update(error_type=error_type, error=message)
        return _redact_llm_result(result, config)

    try:
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
        if response_format is not None:
            if not isinstance(response_format, dict) or response_format.get("type") not in {"text", "json_object", "json_schema"}:
                return finish("invalid_response_format", "response_format must specify text, json_object or json_schema")
            if response_format["type"] == "json_schema":
                spec = response_format.get("json_schema")
                if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict) or not isinstance(spec.get("name"), str):
                    return finish("invalid_response_format", "json_schema requires a name and schema object")
            payload["response_format"] = copy.deepcopy(response_format)
        elif json_mode and not config.openai_model.startswith("gpt-5"):
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        metadata: dict = {}
        data = _read_chat_completion_json(req, label="LLM request", response_metadata=metadata)
        result.update(metadata)
        if not isinstance(data, dict):
            return finish("invalid_response", "Chat completion envelope must be an object")
        for target, key in (("response_id", "id"), ("request_id", "request_id")):
            if isinstance(data.get(key), str):
                result[target] = data[key]
        if isinstance(data.get("usage"), dict) and data["usage"]:
            result.update(usage=data["usage"], usage_status="available")
        if data.get("error"):
            return finish("api_error", json.dumps(data["error"], ensure_ascii=False))
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return finish("invalid_response", "Chat completion requires a non-empty choices array of objects")
        choice = choices[0]
        reason = choice.get("finish_reason")
        if reason is not None and not isinstance(reason, str):
            return finish("invalid_response", "finish_reason must be a string or null")
        result["finish_reason"] = reason
        message = choice.get("message")
        if not isinstance(message, dict):
            return finish("invalid_response", "choices[0].message must be an object")
        result["refusal"] = message.get("refusal")
        text = message.get("content")
        if isinstance(text, str):
            result["text"] = text
        if result["refusal"]:
            return finish("refusal", "Model refused to produce the requested output")
        if reason == "length":
            return finish("truncated_response", "Model output was truncated (finish_reason=length)")
        if reason == "content_filter":
            return finish("content_filter", "Model output was filtered")
        if not isinstance(text, str):
            return finish("invalid_response", "choices[0].message.content must be a string")
        should_parse = response_format.get("type") != "text" if response_format is not None else json_mode
        if should_parse:
            try:
                parsed = json.loads(text, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                result["parse_error"] = str(exc)
                if response_format is not None:
                    return finish("invalid_json", "Model output is not valid JSON: " + str(exc))
            else:
                result["parsed_payload"] = parsed
                if not isinstance(parsed, dict):
                    return finish("invalid_output_shape", "Model JSON output must be an object, not an array or scalar")
                result["json_payload"] = parsed
        return finish()
    except LLMTimeoutError as exc:
        raise LLMTimeoutError(_redact_llm_result(str(exc), config)) from None
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        if exc.headers:
            result["request_id"] = exc.headers.get("x-request-id")
        # Never include the exception URL or request headers, which may contain credentials.
        try:
            detail = exc.read(8192).decode("utf-8", errors="replace")
        except Exception:
            detail = "HTTP request rejected"
        finally:
            exc.close()
        return finish("http_error", f"LLM HTTP {exc.code}: {detail}")
    except Exception as e:
        return finish("transport_error" if isinstance(e, (ConnectionError, OSError)) else "invalid_response", str(e))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


def _redact_llm_result(value: Any, config: RuntimeConfig) -> Any:
    """Sanitize response/error data before callers persist it in traces."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if str(key).lower() in {
            "authorization", "api_key", "password", "mysql_password", "openai_api_key",
        } else _redact_llm_result(item, config)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_llm_result(item, config) for item in value]
    if isinstance(value, str):
        for secret in (config.openai_api_key, config.mysql_password):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s\"',}]+",
                       lambda match: match[1] + " [REDACTED]", value)
        value = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(
            r'''(?i)(["']?(?:authorization|api_key|password|mysql_password|openai_api_key)["']?\s*[:=]\s*)'''
            r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,&}]+)''',
            lambda match: match[1] + '"[REDACTED]"', value,
        )
        return value
    return value


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
