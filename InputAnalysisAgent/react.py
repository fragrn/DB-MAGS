"""Native tool-calling planner for post-driven reproduction blueprints."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent import tools as agent_tools
from agent.config import RuntimeConfig

from InputAnalysisAgent.schemas import ReproductionBlueprint, ReproductionEvaluation


class ReproductionPlanningError(RuntimeError):
    def __init__(self, reason: str, trace: list[dict[str, Any]] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.trace = trace or []


LLM_TIMEOUT_SEC = 180
LLM_MAX_ATTEMPTS = 2


SYSTEM_PROMPT = """You are InputAnalysisAgent, an autonomous database incident reproduction planner.

Read the DBA post, distinguish explicit facts from hypotheses and your assumptions, and design a
mechanism-level reproduction. The original production data is unavailable: design synthetic data
constraints that reproduce cardinality, skew, selectivity, join fanout, indexes, and statistics
needed by the suspected mechanism. Do not claim exact accident reproduction.

Use tools to inspect available capabilities and the target database when useful. Use EXPLAIN for
SQL that can be checked against an existing schema. Tools are capabilities, not reproduction
templates. You decide the schema, distributions, workload, SQL, task parameters, and evaluation
criteria from evidence in the post. Do not invent a MySQL substitute for a DBMS-specific mechanism;
mark feasibility blocked or symptom_only when the required environment is unavailable.
Before changing any MySQL global variable, call inspect_mysql_global_variables and use the returned
pre-experiment values in cleanup_actions.

Return one JSON object matching this contract:
{
  "incident_spec": {
    "dbms": "mysql/sqlserver/postgresql/unknown",
    "dbms_version": null,
    "summary": "...",
    "symptoms": ["..."],
    "mechanism": "...",
    "facts": [{"key":"...","value":{},"source":"explicit_post|post_hypothesis|agent_inference|human_input","evidence":"...","confidence":0.0}],
    "assumptions": ["..."], "unknowns": ["..."], "confidence": 0.0
  },
  "feasibility": {
    "level":"exact|mechanism|symptom_only|blocked", "rationale":"...",
    "missing_capabilities":[], "unmatched_conditions":[], "confidence":0.0
  },
  "environment_spec": {"dbms":"...","database":"...","requirements":[],"isolation":"dedicated_test_database"},
  "data_spec": {
    "database":"...", "schema_sql":["one idempotent statement per item, use IF NOT EXISTS"],
    "generation_sql":["bounded/idempotent SQL statement per item; {row_count} may be used"], "tables":[],
    "constraints":{"cardinality":{},"value_skew":{},"predicate_selectivity":{},"join_selectivity":{}},
    "analyze_tables":[],
    "calibration_queries":[{"sql":"...","expected_plan_features":["..."],"max_probe_sec":10}],
    "scale_strategy":{"initial_rows":1000,"max_rows":1000000,"growth_factor":2.0,"max_rounds":3}
  },
  "workload_spec": {"enabled":false,"method":"none|sql|benchbase","queries":[],"concurrency":1,"duration_sec":30},
  "evaluation_spec": {
    "validation_criteria":["..."], "symptom_evidence":[], "mechanism_evidence":[],
    "minimum_plan_similarity":0.6
  },
  "experiment_request": {
    "target_anomaly":"...","target_database":"...","dba_description":"...",
    "target_path":[],"injected_nodes":[],"max_duration_sec":60,"max_retry_rounds":2,
    "risk_level":"low|medium|high","safety_overrides":{},"workload":{"enabled":false}
  },
  "task_specs": [{
    "task_id":"...","task_type":"...","actions":[],"expected_metrics":{},
    "success_criteria":{},"risk_assessment":"low|medium|high",
    "metadata":{"root_cause":"..."}
  }],
  "dependencies": [["source_task","target_task"]],
  "risk_assessment":"low|medium|high",
  "requires_domain_judgment":false,
  "unresolved_critical_questions":[],
  "rationale":"..."
}

Task actions must use the current executor's raw action kinds: raw_sql_workload,
raw_transaction_script, raw_command, logical_backup_command, or benchbase_burst_command.
Commands must be argv arrays. Do not emit destructive SQL, production targets, unbounded data
generation, or shell strings. Global configuration changes must be TaskSpec actions, never
schema_sql/generation_sql, and the TaskSpec must include cleanup_actions that restore every
changed variable to its inspected pre-experiment value. Return JSON only after tool use is complete.
"""


def plan_reproduction(
    post: str,
    *,
    metadata: dict[str, Any] | None = None,
    config: RuntimeConfig | None = None,
    feedback: str = "",
    previous_blueprint: dict[str, Any] | None = None,
    observations: dict[str, Any] | None = None,
    max_steps: int = 15,
) -> tuple[ReproductionBlueprint, list[dict[str, Any]]]:
    config = config or RuntimeConfig.from_env()
    if not config.openai_api_key or not config.planner_enabled:
        raise ReproductionPlanningError("planner disabled or missing OPENAI_API_KEY")
    user_prompt = json.dumps(
        {
            "dba_forum_post": post,
            "metadata": metadata or {},
            "human_feedback": feedback,
            "previous_blueprint": previous_blueprint,
            "calibration_observations": observations,
        },
        ensure_ascii=False,
        indent=2,
    )
    result = _tool_loop(config, SYSTEM_PROMPT, user_prompt, max_steps=max_steps)
    if result.get("error"):
        raise ReproductionPlanningError(str(result["error"]), result.get("trace"))
    try:
        blueprint = ReproductionBlueprint.from_dict(result["json_payload"])
    except (KeyError, ValueError) as exc:
        raise ReproductionPlanningError(f"invalid reproduction blueprint: {exc}", result.get("trace")) from exc
    return blueprint, result.get("trace", [])


def evaluate_reproduction(
    blueprint: ReproductionBlueprint,
    observations: dict[str, Any],
    *,
    config: RuntimeConfig,
) -> ReproductionEvaluation:
    prompt = """Evaluate whether a database reproduction hit both the reported symptom and its
mechanism. Exact elapsed time is not required. Use only supplied observations. Return JSON with
symptom_hit, mechanism_hit, plan_similarity (0..1), success, reason, unmatched_conditions, evidence.
success requires symptom_hit and mechanism_hit and plan_similarity meeting evaluation_spec."""
    response = agent_tools.llm_generate(
        config=config,
        system_prompt=prompt,
        user_prompt=json.dumps({"blueprint": blueprint.to_dict(), "observations": observations}, ensure_ascii=False),
        temperature=0.0,
        json_mode=True,
    )
    if response.get("error") or not isinstance(response.get("json_payload"), dict):
        raise ReproductionPlanningError(f"evaluation LLM failed: {response.get('error') or 'invalid JSON'}")
    return ReproductionEvaluation.from_dict(response["json_payload"])


def _tool_loop(config: RuntimeConfig, system_prompt: str, user_prompt: str, *, max_steps: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[dict[str, Any]] = []
    schemas = _tool_schemas()
    for step in range(1, max_steps + 1):
        payload: dict[str, Any] = {
            "model": config.openai_model,
            "messages": messages,
            "tools": schemas,
            "tool_choice": "auto",
        }
        if not config.openai_model.startswith("gpt-5"):
            payload["temperature"] = config.planner_temperature
        request = urllib.request.Request(
            f"{config.openai_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.openai_api_key}"},
            method="POST",
        )
        raw, request_error = _request_with_retry(request, config=config, step=step, trace=trace)
        if request_error:
            return {"error": request_error, "trace": trace}
        message = ((raw.get("choices") or [{}])[0].get("message") or {})
        messages.append(message)
        calls = message.get("tool_calls") or []
        if calls:
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
                    result = _call_tool(name, arguments, config)
                    observation = {"result": result}
                    trace.append({"step": step, "tool": name, "arguments": arguments, "result": _truncate(result)})
                except Exception as exc:
                    observation = {"error": str(exc)}
                    trace.append({"step": step, "tool": name, "arguments": raw_arguments, "error": str(exc)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(_truncate(observation), ensure_ascii=False),
                })
            continue
        text = str(message.get("content") or "")
        trace.append({"step": step, "tool": "final_answer", "content": text[:4000]})
        try:
            return {"json_payload": json.loads(text), "trace": trace}
        except json.JSONDecodeError as exc:
            return {"error": f"final JSON parse failed: {exc}", "trace": trace}
    return {"error": f"tool calling exceeded max_steps={max_steps}", "trace": trace}


def _request_with_retry(
    request: urllib.request.Request,
    *,
    config: RuntimeConfig,
    step: int,
    trace: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    attempts = max(1, int(config.input_analysis_llm_max_attempts or LLM_MAX_ATTEMPTS))
    timeout_sec = max(1, int(config.input_analysis_llm_timeout_sec or LLM_TIMEOUT_SEC))
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                return json.loads(response.read().decode("utf-8")), ""
        except (TimeoutError, socket.timeout) as exc:
            trace.append({
                "step": step,
                "event": "request_timeout",
                "attempt": attempt,
                "timeout_sec": timeout_sec,
                "error": str(exc),
            })
            if attempt < attempts:
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            return {}, (
                f"tool-calling request timed out at step {step} after "
                f"{attempts} attempt(s) x {timeout_sec}s: {exc}"
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                trace.append({
                    "step": step,
                    "event": "request_timeout",
                    "attempt": attempt,
                    "timeout_sec": timeout_sec,
                    "error": str(exc),
                })
                if attempt < attempts:
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
            return {}, f"tool-calling request failed at step {step}, attempt {attempt}: {exc}"
        except Exception as exc:
            return {}, f"tool-calling request failed at step {step}, attempt {attempt}: {exc}"
    return {}, f"tool-calling request failed at step {step}"


def _call_tool(name: str, arguments: dict[str, Any], config: RuntimeConfig) -> Any:
    tools: dict[str, Callable[..., Any]] = {
        "inspect_capabilities": lambda: _inspect_capabilities(config),
        "inspect_mysql_global_variables": lambda: _inspect_mysql_global_variables(config),
        "inspect_db_environment": lambda database: _inspect_db_environment(config, database),
        "explain_sql": lambda database, sql: agent_tools.explain_sql(config, database, sql),
        "compile_task_specs": lambda task_specs, dependencies=None: agent_tools.build_task_dag(task_specs, dependencies or []),
        "check_safety": lambda task_dag, max_duration_sec=300, expected_workload=None: agent_tools.check_safety(
            task_dag,
            config,
            max_duration_sec=max_duration_sec,
            expected_workload=expected_workload or {},
        ),
    }
    if name not in tools:
        raise ValueError(f"tool '{name}' is not allowed")
    return tools[name](**arguments)


def _inspect_capabilities(config: RuntimeConfig) -> dict[str, Any]:
    return {
        "dbms_adapters": ["mysql"],
        "task_action_kinds": [
            "raw_sql_workload",
            "raw_transaction_script",
            "raw_command",
            "logical_backup_command",
            "benchbase_burst_command",
        ],
        "mysql": {"host": config.mysql_host, "port": config.mysql_port},
        "chaosblade_available": Path(config.chaosblade_path).exists(),
        "limitations": ["No SQL Server/PostgreSQL execution adapter in v1", "No production data dump is assumed"],
    }


def _inspect_db_environment(config: RuntimeConfig, database: str) -> dict[str, Any]:
    return {
        "schema": agent_tools.probe_schema(config, database),
        "table_stats": agent_tools.probe_table_stats(config, database),
        "db_metrics": agent_tools.probe_db_metrics(config, database),
        "os_metrics": agent_tools.probe_os_metrics(),
    }


def _inspect_mysql_global_variables(config: RuntimeConfig) -> dict[str, Any]:
    from InputAnalysisAgent.slowlog import SlowLogProbe

    marker = SlowLogProbe(config).marker()
    return {
        "variables": marker.get("variables", {}),
        "error": marker.get("variable_error", ""),
    }


def _tool_schemas() -> list[dict[str, Any]]:
    definitions = {
        "inspect_capabilities": ("Inspect DBMS adapters and executor capabilities.", {}, []),
        "inspect_mysql_global_variables": (
            "Read current MySQL slow-log global variables before planning changes and cleanup.",
            {},
            [],
        ),
        "inspect_db_environment": ("Inspect an existing MySQL database.", {"database": {"type": "string"}}, ["database"]),
        "explain_sql": ("Run EXPLAIN for a read-only SQL statement.", {"database": {"type": "string"}, "sql": {"type": "string"}}, ["database", "sql"]),
        "compile_task_specs": (
            "Compile direct TaskSpecs into a DAG without executing it.",
            {
                "task_specs": {"type": "array", "items": {"type": "object"}},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                },
            },
            ["task_specs"],
        ),
        "check_safety": (
            "Validate a compiled Task DAG against hard safety limits.",
            {"task_dag": {"type": "object"}, "max_duration_sec": {"type": "number"}, "expected_workload": {"type": "object"}},
            ["task_dag"],
        ),
    }
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            },
        }
        for name, (description, properties, required) in definitions.items()
    ]


def _truncate(value: Any, limit: int = 16000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}
