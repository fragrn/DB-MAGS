"""Native tool-calling planner for post-driven reproduction blueprints."""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent import tools as agent_tools
from agent.config import RuntimeConfig

from InputAnalysisAgent.db_adapters import PostgresAdapter, SqlServerAdapter, normalize_dbms
from InputAnalysisAgent.prompt_examples import DATASPEC_FORMAT_REFERENCE
from InputAnalysisAgent.schemas import ReproductionBlueprint, ReproductionEvaluation


class ReproductionPlanningError(RuntimeError):
    def __init__(
        self,
        reason: str,
        trace: list[dict[str, Any]] | None = None,
        candidate: dict[str, Any] | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.trace = trace or []
        self.candidate = candidate


LLM_TIMEOUT_SEC = 300
LLM_MAX_ATTEMPTS = 2


SYSTEM_PROMPT = """You are InputAnalysisAgent, an autonomous database incident reproduction planner.

Read the DBA post, distinguish explicit facts from hypotheses and your assumptions, and design a
mechanism-level reproduction. The original production data is unavailable: design synthetic data
constraints that reproduce cardinality, skew, selectivity, join fanout, indexes, and statistics
needed by the suspected mechanism. Do not claim exact accident reproduction.

Use tools to inspect available capabilities and the target database when useful. Use EXPLAIN for
SQL that can be checked against an existing schema. Tools are capabilities, not reproduction
templates. You decide the schema, distributions, workload, SQL, task parameters, and evaluation
criteria from evidence in the post. The current execution adapters are MySQL/MariaDB/Percona,
PostgreSQL, and a SQL Server-compatible T-SQL adapter backed by the local Azure SQL Edge/sqlcmd
environment. Do not invent a MySQL substitute for a DBMS-specific mechanism; mark feasibility
blocked or symptom_only when the required environment is unavailable.
If metadata.target_database is provided, use that exact database name for data_spec.database,
environment_spec.database, experiment_request.target_database, and every TaskSpec action database.
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
    "generation_sql":["bounded/idempotent SQL statement per item; {row_count} may be used"],
    "tables":[{"name":"...","purpose":"...","target_rows":1000,"distribution_notes":"..."}],
    "constraints":{"cardinality":{},"value_skew":{},"predicate_selectivity":{},"join_selectivity":{}},
    "analyze_tables":[],
    "calibration_queries":[{"sql":"...","objective":"...","expected_evidence":["..."]}],
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
For raw_sql_workload, sql must be one SQL string (never an array/object), concurrency must be a
positive integer, and duration_sec must be positive. To run multiple SQL statements, use separate
actions or raw_transaction_script; each transaction step.sql must also be one string.
All SQL must be immediately executable against the synthetic database. Never use unresolved
parameter placeholders such as ?, $1, :name, or @p1 in schema_sql, generation_sql,
calibration_queries, workload queries, TaskSpec SQL, or transaction script SQL.
raw_transaction_script must use this exact shape:
{"kind":"raw_transaction_script","database":"...","duration_sec":1,
 "scripts":[{"role":"configure","autocommit":true,"concurrency":1,
 "steps":[{"sql":"SET ..."}]}]}.
For raw_command, logical_backup_command, and benchbase_burst_command, the field name must be
"command". Its value must be a non-empty array of strings representing the executable and its
arguments. Never use "argv" as a field name. Use this exact raw_command shape:
{"kind":"raw_command","command":["grep","-F","repro_events","/path/to/slow.log"],
"duration_sec":1,"cleanup_command":["executable","arg1"]}.
Do not put steps directly on the action. calibration_queries.sql must be the original SELECT/WITH
query without an EXPLAIN prefix; the runtime adds EXPLAIN. generation_sql must contain executable,
bounded SQL only, never comments or prose.
Use this retrieved format reference for the required data_spec JSON shape. It constrains format only;
do not copy its table names, SQL semantics, data distribution, or evaluation intent:
__DATASPEC_FORMAT_REFERENCE__
Calibration queries describe natural-language objectives and expected EXPLAIN evidence. Do not use
conditions, metrics, operators, fixed access-method enums, probe latency, runtime configuration,
slow-log eligibility, or final anomaly outcomes in calibration. Those belong in TaskSpecs and
evaluation_spec. The post-preparation Calibration ReAct loop will call EXPLAIN and judge the evidence.
Do not emit destructive SQL, production targets, unbounded data
generation, or shell strings. Global configuration changes must be TaskSpec actions, never
schema_sql/generation_sql, and the TaskSpec must include cleanup_actions that restore every
changed variable to its inspected pre-experiment value. Return JSON only after tool use is complete.
""".replace("__DATASPEC_FORMAT_REFERENCE__", DATASPEC_FORMAT_REFERENCE)


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
        candidate, normalization = canonicalize_blueprint_payload(result["json_payload"])
        if normalization:
            result.setdefault("trace", []).append({
                "step": "final_validation",
                "event": "canonicalized_blueprint",
                "changes": normalization,
            })
        blueprint = ReproductionBlueprint.from_dict(candidate)
    except (KeyError, ValueError) as exc:
        raise ReproductionPlanningError(
            f"invalid reproduction blueprint: {exc}",
            result.get("trace"),
            candidate if "candidate" in locals() else (
                result.get("json_payload") if isinstance(result.get("json_payload"), dict) else None
            ),
        ) from exc
    return blueprint, result.get("trace", [])


CALIBRATION_SYSTEM_PROMPT = """You are the Calibration phase of InputAnalysisAgent.

The synthetic database has already been prepared from the supplied reproduction blueprint. For every
calibration query, you must call explain_sql against the prepared database. Pass its query_id unchanged
and pass only the original SELECT/WITH statement in sql. Never prepend EXPLAIN because the tool adds it.
Preserve the supplied SQL, including optimizer hints. Read the raw DBMS EXPLAIN
result and decide yourself whether it supports the query's objective and expected_evidence. There is no
rule-based plan matcher and no fixed access-method vocabulary. Do not execute workload tasks, mutate the
database, or claim evidence that was not returned by explain_sql.

Return JSON only after all required EXPLAIN calls:
{
  "decision":"accept|weak_match|reject",
  "reasoning":"...",
  "query_assessments":[{
    "query_id":"calibration_query_1", "sql":"...", "matched":true, "observed_plan_summary":"...",
    "supporting_evidence":["..."], "discrepancies":[]
  }],
  "concerns":[],
  "recommended_changes":[],
  "missing_information":[]
}

Use accept when the observed plans support the intended mechanism well enough to execute. Use
weak_match when the plan does not fully match but still shows enough mechanism evidence that execution
may produce the target anomaly; include concerns and recommended_changes for DBA review. Use reject
when EXPLAIN fails, required objects are missing, the plan is completely unrelated to the target
mechanism, or execution is unlikely to produce the target anomaly. Do not return a revised_blueprint
from calibration; DBA revise/feedback controls blueprint changes after calibration review.
Calibration observations belong in query_assessments and must not be added as incident facts.
If later asked to revise through feedback, keep data_spec in the object-array format required by this
retrieved format reference:
__DATASPEC_FORMAT_REFERENCE__
""".replace("__DATASPEC_FORMAT_REFERENCE__", DATASPEC_FORMAT_REFERENCE)


def calibrate_reproduction(
    post: str,
    blueprint: ReproductionBlueprint,
    preparation_result: dict[str, Any],
    *,
    round_no: int,
    previous_calibration: dict[str, Any] | None = None,
    config: RuntimeConfig,
    max_steps: int = 15,
) -> tuple[dict[str, Any], None, list[dict[str, Any]]]:
    """Let the LLM inspect real EXPLAIN output and make the calibration decision."""
    if not config.openai_api_key or not config.planner_enabled:
        raise ReproductionPlanningError("calibration planner disabled or missing OPENAI_API_KEY")
    queries = list(blueprint.data_spec.calibration_queries)
    identified_queries = [
        {"query_id": f"calibration_query_{index}", **query}
        for index, query in enumerate(queries, start=1)
    ]
    user_prompt = json.dumps({
        "dba_forum_post": post,
        "round": round_no,
        "preparation_result": preparation_result,
        "calibration_queries": identified_queries,
        "current_blueprint": blueprint.to_dict(),
        "previous_calibration": previous_calibration,
    }, ensure_ascii=False, indent=2)
    result = _tool_loop(
        config,
        CALIBRATION_SYSTEM_PROMPT,
        user_prompt,
        max_steps=max_steps,
        tool_names={"explain_sql"},
        calibration_queries={item["query_id"]: item["sql"] for item in identified_queries},
        dbms=blueprint.incident_spec.dbms,
    )
    if result.get("error"):
        raise ReproductionPlanningError(str(result["error"]), result.get("trace"))
    payload = result.get("json_payload")
    trace = result.get("trace", [])
    if not isinstance(payload, dict):
        raise ReproductionPlanningError("invalid calibration result: expected JSON object", trace)
    _validate_calibration_result(payload, queries, trace)

    return payload, None, trace


def _validate_calibration_result(
    payload: dict[str, Any],
    queries: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> None:
    decision = str(payload.get("decision") or "")
    if decision not in {"accept", "weak_match", "reject"}:
        raise ReproductionPlanningError("invalid calibration decision", trace)
    if not str(payload.get("reasoning") or "").strip():
        raise ReproductionPlanningError("calibration reasoning is required", trace)
    assessments = payload.get("query_assessments")
    if not isinstance(assessments, list):
        raise ReproductionPlanningError("calibration query_assessments must be an array", trace)
    expected = [
        (f"calibration_query_{index}", str(item["sql"]).strip())
        for index, item in enumerate(queries, start=1)
    ]
    explain_calls = [item for item in trace if item.get("tool") == "explain_sql"]

    def call_matches(query_id: str, sql: str, call: dict[str, Any]) -> bool:
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, dict):
            return False
        supplied_id = str(arguments.get("query_id") or "").strip()
        return (
            (not supplied_id or supplied_id == query_id)
            and _normalize_sql_identity(arguments.get("sql")) == _normalize_sql_identity(sql)
        )

    missing_calls = [sql for query_id, sql in expected if not any(call_matches(query_id, sql, call) for call in explain_calls)]
    if missing_calls:
        raise ReproductionPlanningError(
            "calibration must call explain_sql for every query: " + "; ".join(missing_calls),
            trace,
        )
    if decision in {"accept", "weak_match"}:
        successful_calls = [
            item for item in explain_calls
            if "result" in item and not item.get("error")
        ]
        failed_calls = [
            sql for query_id, sql in expected
            if not any(call_matches(query_id, sql, call) for call in successful_calls)
        ]
        if failed_calls:
            raise ReproductionPlanningError(
                f"{decision} calibration requires successful explain_sql results: " + "; ".join(failed_calls),
                trace,
            )
    def assessment_matches(query_id: str, sql: str, assessment: Any) -> bool:
        if not isinstance(assessment, dict):
            return False
        supplied_id = str(assessment.get("query_id") or "").strip()
        return (
            (not supplied_id or supplied_id == query_id)
            and _normalize_sql_identity(assessment.get("sql")) == _normalize_sql_identity(sql)
        )

    missing_assessments = [
        sql for query_id, sql in expected
        if not any(assessment_matches(query_id, sql, assessment) for assessment in assessments)
    ]
    if missing_assessments:
        raise ReproductionPlanningError(
            "calibration must assess every query: " + "; ".join(missing_assessments),
            trace,
        )
    for index, assessment in enumerate(assessments):
        if not isinstance(assessment, dict):
            raise ReproductionPlanningError(f"query_assessments[{index}] must be an object", trace)
        if not isinstance(assessment.get("matched"), bool):
            raise ReproductionPlanningError(f"query_assessments[{index}].matched must be boolean", trace)
        if decision != "reject" and not str(assessment.get("observed_plan_summary") or "").strip():
            raise ReproductionPlanningError(
                f"query_assessments[{index}].observed_plan_summary is required",
                trace,
            )
        for field in ("supporting_evidence", "discrepancies"):
            value = assessment.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ReproductionPlanningError(f"query_assessments[{index}].{field} must be a string array", trace)
    if decision == "accept" and not all(bool(item.get("matched")) for item in assessments):
        raise ReproductionPlanningError("accept calibration decision conflicts with query assessments", trace)
    for field in ("concerns", "recommended_changes", "missing_information"):
        value = payload.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ReproductionPlanningError(f"calibration {field} must be a string array", trace)


def _normalize_sql_identity(value: Any) -> str:
    """Normalize harmless presentation differences for calibration coverage only."""
    sql = str(value or "").strip()
    sql = re.sub(r"^EXPLAIN\s+(?:FORMAT\s*=\s*\w+\s+)?", "", sql, flags=re.I)
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").casefold()


def canonicalize_blueprint_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize lossless LLM shorthand without making reproduction decisions."""
    candidate = copy.deepcopy(payload)
    changes: list[str] = []
    _normalize_confidence_fields(candidate, changes)
    _normalize_fact_sources(candidate, changes)
    data_spec = candidate.get("data_spec") or {}
    scale = data_spec.get("scale_strategy")
    if not isinstance(scale, dict):
        data_spec["scale_strategy"] = {"initial_rows": 1000, "max_rows": 10000, "growth_factor": 2.0, "max_rounds": 2}
        changes.append("added default data_spec.scale_strategy")
    else:
        before = copy.deepcopy(scale)
        try:
            scale["initial_rows"] = max(1, int(scale.get("initial_rows") or 1000))
            scale["max_rows"] = max(scale["initial_rows"], int(scale.get("max_rows") or scale["initial_rows"]))
            scale["growth_factor"] = float(scale.get("growth_factor") or 2.0)
            if scale["growth_factor"] <= 1:
                scale["growth_factor"] = 2.0
            scale["max_rounds"] = max(1, int(scale.get("max_rounds") or 1))
        except (TypeError, ValueError):
            data_spec["scale_strategy"] = {"initial_rows": 1000, "max_rows": 10000, "growth_factor": 2.0, "max_rounds": 2}
        if before != data_spec.get("scale_strategy"):
            changes.append("normalized data_spec.scale_strategy")
    generation_sql = data_spec.get("generation_sql")
    if isinstance(generation_sql, list):
        filtered = [
            sql for sql in generation_sql
            if not (isinstance(sql, str) and sql.lstrip().startswith(("--", "#")))
        ]
        if len(filtered) != len(generation_sql):
            data_spec["generation_sql"] = filtered
            changes.append("removed comment-only generation_sql entries")
    calibration_queries = data_spec.get("calibration_queries") or []
    if isinstance(calibration_queries, list):
        filtered_calibrations = []
        for index, item in enumerate(calibration_queries):
            if isinstance(item, dict) and isinstance(item.get("sql"), str):
                normalized_sql = re.sub(r"^\s*EXPLAIN(?:\s+FORMAT\s*=\s*JSON)?\s+", "", item["sql"], flags=re.I)
                if normalized_sql != item["sql"]:
                    item["sql"] = normalized_sql
                    changes.append(f"stripped EXPLAIN prefix from data_spec.calibration_queries[{index}].sql")
                if not re.match(r"^\s*(SELECT|WITH)\b", item["sql"], re.I):
                    changes.append(f"removed non-read-only data_spec.calibration_queries[{index}]")
                    continue
            filtered_calibrations.append(item)
        if len(filtered_calibrations) != len(calibration_queries):
            data_spec["calibration_queries"] = filtered_calibrations
    for index, item in enumerate(data_spec.get("calibration_queries") or []):
        if isinstance(item, dict) and isinstance(item.get("sql"), str):
            normalized = re.sub(r"^\s*EXPLAIN(?:\s+FORMAT\s*=\s*JSON)?\s+", "", item["sql"], flags=re.I)
            if normalized != item["sql"]:
                item["sql"] = normalized
                changes.append(f"stripped EXPLAIN prefix from data_spec.calibration_queries[{index}].sql")
    for task_index, task in enumerate(candidate.get("task_specs") or []):
        if not isinstance(task, dict):
            continue
        for section in ("actions", "cleanup_actions"):
            for action_index, action in enumerate(task.get(section) or []):
                if not isinstance(action, dict) or action.get("kind") != "raw_transaction_script":
                    continue
                if not action.get("scripts") and isinstance(action.get("steps"), list) and action["steps"]:
                    steps = action.pop("steps")
                    script = {
                        "role": str(action.pop("role", "transaction_script")),
                        "autocommit": bool(action.pop("autocommit", True)),
                        "concurrency": int(action.pop("concurrency", 1) or 1),
                        "steps": steps,
                    }
                    action["scripts"] = [script]
                    changes.append(
                        f"wrapped task_specs[{task_index}].{section}[{action_index}].steps in scripts[]"
                    )
    return candidate, changes


def _normalize_confidence_fields(value: Any, changes: list[str], path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in list(value.items()):
            child_path = f"{path}.{key}" if path else key
            if key == "confidence" and child is None:
                value[key] = 0.5
                changes.append(f"defaulted missing {child_path} to 0.5")
            else:
                _normalize_confidence_fields(child, changes, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _normalize_confidence_fields(child, changes, f"{path}[{index}]")


def _normalize_fact_sources(value: Any, changes: list[str], path: str = "") -> None:
    valid_sources = {"explicit_post", "post_hypothesis", "agent_inference", "human_input"}
    if isinstance(value, dict):
        for key, child in list(value.items()):
            child_path = f"{path}.{key}" if path else key
            if key == "source" and path.endswith("facts[]"):
                source = str(child or "").strip()
                if source not in valid_sources:
                    value[key] = "agent_inference"
                    changes.append(f"normalized invalid {child_path}={source!r} to agent_inference")
            else:
                _normalize_fact_sources(child, changes, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[]" if path.endswith("facts") else f"{path}[{index}]"
            _normalize_fact_sources(child, changes, child_path)


def evaluate_reproduction(
    blueprint: ReproductionBlueprint,
    observations: dict[str, Any],
    *,
    config: RuntimeConfig,
) -> ReproductionEvaluation:
    prompt = """Evaluate whether a database reproduction hit both the reported symptom and its
mechanism. Exact elapsed time is not required. Use only supplied observations. Return exactly one
JSON object with this shape:
{
  "symptom_hit": true,
  "mechanism_hit": true,
  "plan_similarity": 0.0,
  "success": true,
  "reason": "...",
  "unmatched_conditions": ["..."],
  "evidence": {"descriptive_evidence_name": "value or structured observation"}
}
evidence must always be a JSON object, never an array, string, boolean, or null. success requires
symptom_hit and mechanism_hit and plan_similarity meeting evaluation_spec."""
    response = agent_tools.llm_generate(
        config=config,
        system_prompt=prompt,
        user_prompt=json.dumps({"blueprint": blueprint.to_dict(), "observations": observations}, ensure_ascii=False),
        temperature=0.0,
        json_mode=True,
    )
    if response.get("error") or not isinstance(response.get("json_payload"), dict):
        raise ReproductionPlanningError(f"evaluation LLM failed: {response.get('error') or 'invalid JSON'}")
    payload = _canonicalize_evaluation_payload(response["json_payload"])
    try:
        return ReproductionEvaluation.from_dict(payload)
    except ValueError as exc:
        raise ReproductionPlanningError(f"invalid evaluation result: {exc}", candidate=payload) from exc


def _canonicalize_evaluation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve LLM evidence while adapting common JSON container shorthand."""
    candidate = copy.deepcopy(payload)
    evidence = candidate.get("evidence")
    if evidence is None:
        candidate["evidence"] = {}
    elif isinstance(evidence, list):
        candidate["evidence"] = {"items": evidence}
    elif isinstance(evidence, str):
        candidate["evidence"] = {"summary": evidence}
    return candidate


def _tool_loop(
    config: RuntimeConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    max_steps: int,
    tool_names: set[str] | None = None,
    calibration_queries: dict[str, str] | None = None,
    dbms: str | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[dict[str, Any]] = []
    schemas = _tool_schemas(tool_names)
    for step in range(1, max_steps + 1):
        payload: dict[str, Any] = {
            "model": config.openai_model,
            "messages": messages,
            "tools": schemas,
            "tool_choice": "auto",
        }
        if not config.openai_model.startswith("gpt-5"):
            payload["temperature"] = config.planner_temperature
            payload["response_format"] = {"type": "json_object"}
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
                    if name == "explain_sql":
                        query_id = str(arguments.get("query_id") or "").strip()
                        if calibration_queries is not None and query_id:
                            if query_id not in calibration_queries:
                                raise ValueError(f"unknown calibration query_id: {query_id}")
                            arguments["sql"] = calibration_queries[query_id]
                        else:
                            arguments["sql"] = re.sub(
                                r"^\s*EXPLAIN(?:\s+FORMAT\s*=\s*\w+)?\s+",
                                "",
                                str(arguments.get("sql") or ""),
                                flags=re.I,
                            )
                    result = _call_tool(name, arguments, config, allowed_tools=tool_names, dbms=dbms)
                    observation = {"result": result}
                    trace.append({"step": step, "tool": name, "arguments": arguments, "result": _truncate(result)})
                except Exception as exc:
                    observation = {"error": str(exc)}
                    trace.append({"step": step, "tool": name, "arguments": arguments if "arguments" in locals() else raw_arguments, "error": str(exc)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(_truncate(observation), ensure_ascii=False),
                })
            continue
        text = str(message.get("content") or "")
        trace.append({"step": step, "tool": "final_answer", "content": text[:4000]})
        try:
            return {"json_payload": _parse_json_object_from_text(text), "trace": trace}
        except json.JSONDecodeError as exc:
            return {"error": f"final JSON parse failed: {exc}", "trace": trace}
    return {"error": f"tool calling exceeded max_steps={max_steps}", "trace": trace}


def _parse_json_object_from_text(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        raise json.JSONDecodeError("empty response", stripped, 0)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = json.loads(_extract_json_object(stripped))
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("expected JSON object", stripped, 0)
    return payload


def _extract_json_object(text: str) -> str:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise json.JSONDecodeError("unterminated JSON object", text, start)


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


def _call_tool(
    name: str,
    arguments: dict[str, Any],
    config: RuntimeConfig,
    *,
    allowed_tools: set[str] | None = None,
    dbms: str | None = None,
) -> Any:
    effective_dbms = normalize_dbms(str(arguments.get("dbms") or dbms or "mysql"))
    tools: dict[str, Callable[..., Any]] = {
        "inspect_capabilities": lambda: _inspect_capabilities(config),
        "inspect_mysql_global_variables": lambda: _inspect_mysql_global_variables(config),
        "inspect_db_environment": lambda database, dbms=None: _inspect_db_environment(
            config,
            database,
            dbms=normalize_dbms(dbms or effective_dbms),
        ),
        "explain_sql": lambda database, sql, query_id=None, dbms=None: _explain_sql(
            config,
            database,
            sql,
            dbms=normalize_dbms(dbms or effective_dbms),
        ),
        "compile_task_specs": lambda task_specs, dependencies=None: agent_tools.build_task_dag(task_specs, dependencies or []),
        "check_safety": lambda task_dag, max_duration_sec=300, expected_workload=None: agent_tools.check_safety(
            task_dag,
            config,
            max_duration_sec=max_duration_sec,
            expected_workload=expected_workload or {},
        ),
    }
    if name not in tools or (allowed_tools is not None and name not in allowed_tools):
        raise ValueError(f"tool '{name}' is not allowed")
    return tools[name](**arguments)


def _inspect_capabilities(config: RuntimeConfig) -> dict[str, Any]:
    return {
        "task_action_kinds": [
            "raw_sql_workload",
            "raw_transaction_script",
            "raw_command",
            "logical_backup_command",
            "benchbase_burst_command",
        ],
        "dbms_adapters": ["mysql", "postgresql", "sqlserver"],
        "mysql": {"host": config.mysql_host, "port": config.mysql_port},
        "postgresql": {
            "host": os.environ.get("DBMAGS_PG_HOST") or os.environ.get("PGHOST") or "local socket",
            "port": int(os.environ.get("DBMAGS_PG_PORT") or os.environ.get("PGPORT") or 5432),
            "user": os.environ.get("DBMAGS_PG_USER") or os.environ.get("PGUSER") or "current OS user",
        },
        "sqlserver": {
            "host": os.environ.get("DBMAGS_SQLSERVER_HOST") or "127.0.0.1",
            "port": int(os.environ.get("DBMAGS_SQLSERVER_PORT") or 1433),
            "user": os.environ.get("DBMAGS_SQLSERVER_USER") or "sa",
            "client": os.environ.get("DBMAGS_SQLSERVER_CLIENT") or "docker",
            "compatibility_note": "Local Apple Silicon path uses Azure SQL Edge, not full SQL Server.",
        },
        "chaosblade_available": Path(config.chaosblade_path).exists(),
        "limitations": [
            "Apple Silicon SQL Server path uses Azure SQL Edge compatibility, not full SQL Server 2022.",
            "No production data dump is assumed",
        ],
    }


def _inspect_db_environment(config: RuntimeConfig, database: str, *, dbms: str = "mysql") -> dict[str, Any]:
    if normalize_dbms(dbms) == "postgresql":
        adapter = PostgresAdapter()
        return {
            "database": database,
            "dbms": "postgresql",
            "schema": adapter.schema(database),
            "table_stats": adapter.table_stats(database),
            "db_metrics": adapter.db_metrics(database),
            "os_metrics": agent_tools.probe_os_metrics(),
        }
    if normalize_dbms(dbms) == "sqlserver":
        adapter = SqlServerAdapter()
        return {
            "database": database,
            "dbms": "sqlserver",
            "schema": adapter.schema(database),
            "table_stats": adapter.table_stats(database),
            "db_metrics": adapter.db_metrics(database),
            "os_metrics": agent_tools.probe_os_metrics(),
        }
    return {
        "schema": agent_tools.probe_schema(config, database),
        "table_stats": agent_tools.probe_table_stats(config, database),
        "db_metrics": agent_tools.probe_db_metrics(config, database),
        "os_metrics": agent_tools.probe_os_metrics(),
    }


def _explain_sql(config: RuntimeConfig, database: str, sql: str, *, dbms: str = "mysql") -> dict[str, Any]:
    if normalize_dbms(dbms) == "postgresql":
        return PostgresAdapter().explain(database, sql)
    if normalize_dbms(dbms) == "sqlserver":
        return SqlServerAdapter().explain(database, sql)
    return agent_tools.explain_sql(config, database, sql)


def _inspect_mysql_global_variables(config: RuntimeConfig) -> dict[str, Any]:
    from InputAnalysisAgent.slowlog import SlowLogProbe

    marker = SlowLogProbe(config).marker()
    return {
        "variables": marker.get("variables", {}),
        "error": marker.get("variable_error", ""),
    }


def _tool_schemas(tool_names: set[str] | None = None) -> list[dict[str, Any]]:
    definitions = {
        "inspect_capabilities": ("Inspect DBMS adapters and executor capabilities.", {}, []),
        "inspect_mysql_global_variables": (
            "Read current MySQL slow-log global variables before planning changes and cleanup.",
            {},
            [],
        ),
        "inspect_db_environment": ("Inspect an existing MySQL database.", {"database": {"type": "string"}}, ["database"]),
        "explain_sql": (
            "Run EXPLAIN for a raw SELECT/WITH statement. Do not include the EXPLAIN keyword.",
            {
                "database": {"type": "string"},
                "sql": {"type": "string"},
                "query_id": {"type": "string", "description": "Calibration query_id supplied by the caller."},
            },
            ["database", "sql"],
        ),
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
        if tool_names is None or name in tool_names
    ]


def _truncate(value: Any, limit: int = 16000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}
