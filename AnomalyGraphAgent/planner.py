"""Single-pass global planner with local validation and SQL repair."""

from __future__ import annotations

import json
import copy
import uuid
import re
import time
from functools import wraps
from typing import Any, List, Optional

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.graph import ANOMALY_GRAPH
from AnomalyGraphAgent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExperimentRequest,
    ExecutableTaskDAG,
    PlannerTraceStep,
    ReflectionResult,
    SchemaInfo,
    TaskDAGEdge,
    TaskSpec,
    to_jsonable,
)
from AnomalyGraphAgent import tools as tool_registry
from AnomalyGraphAgent.intent import INTENT_VERSION, IntentValidationError, build_intent_schema, validate_intent_plan, require_compilable_strategy
from AnomalyGraphAgent.intent_compiler import COMPILER_VERSION, CompilationContext, compile_intent_plan
from AnomalyGraphAgent.sql_validation import SQLValidationError, validate_sql
from AnomalyGraphAgent.intent_repair import IntentSemanticError, guard_semantic_repair, guard_sql_repair, guard_partial_repair, sql_slot


class PlannerFallbackError(RuntimeError):
    """Raised when planning would need to fall back to rule-based TaskSpecs."""

    def __init__(
        self,
        reason: str,
        trace: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        stage: str = "planning",
        error_type: str = "planner_error",
    ):
        super().__init__(reason)
        self.reason = reason
        self.trace = trace or []
        self.context = context or {}
        self.stage = self.context.setdefault("stage", stage)
        self.error_type = self.context.setdefault("error_type", error_type)


class PlannerLLMError(PlannerFallbackError):
    """LLM transport/configuration failure; do not spend JSON repair attempts on it."""


def _planner_boundary(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self._stage = "request_validation"
        self._validation_history = []
        self.sql_repair_counts = {}
        self._planner_started = time.perf_counter()
        try:
            return method(self, *args, **kwargs)
        except PlannerFallbackError as exc:
            if exc.stage == "planning":
                exc.stage = self._stage
                exc.context["stage"] = self._stage
            if exc.__cause__ is None:
                exc.__cause__ = RuntimeError(exc.reason)
            raise
        except Exception as exc:
            error_type = "invalid_request" if self._stage == "request_validation" and isinstance(exc, ValueError) else "internal_error"
            self._fail("compiler_error" if self._stage == "compile_intent" else error_type,
                       f"Planner failed during {self._stage}: {type(exc).__name__}: {exc}", cause=exc)
    return wrapped


ALLOWED_PLANNER_ACTION_KINDS = {
    "raw_sql_workload",
    "raw_transaction_script",
    "raw_command",
    "logical_backup_command",
    "benchbase_burst_command",
    "benchbase_burst",
}

RESOURCE_ROOT_CAUSES = {
    "resource_cpu",
    "resource_io",
    "resource_memory",
    "resource_network",
    "network_latency",
    "disk_full_or_pressure",
}


# ---------------------------------------------------------------------------
# System prompt — the core knowledge injected into the global planner
# ---------------------------------------------------------------------------



SINGLE_PASS_SYSTEM_PROMPT = """You plan MySQL anomaly propagation experiments.
Return exactly one object containing intents, one Intent per requested injected node,
using the supplied strict JSON Schema. Each Intent contains only node, strategy,
target, load, sql, expected_evidence. Use null for fields unused by that strategy.
Never change target_path, injected_nodes, target_database, workload identity or safety limits.
Use only registered strategies and identifier_whitelist tables/columns in the target database.
No cross-database access, production targets, destructive DDL, unbounded updates/deletes,
file output, stored procedures or unapproved functions. Do not attempt to fill the system disk.
Duration must be finite, positive and <= duration_limit_sec; hold_sec <= duration_sec.
Strengthen concurrency, rate or hotspot concentration instead of extending the window.
hot_update and long_tx use a SELECT FOR UPDATE holder and UPDATE waiter on the same
existing table and identical predicate. Keep SQL to one statement per slot.
improper_sql, missing_index and large_temp_table use SELECT and must have appropriate
query/index evidence. traffic_surge increases BenchBase concurrency/rate using its existing mix.
table_lock uses a SELECT waiter on the target table. Resource strategies select intensity
from the supplied policy. Other roots require declared compiler and safety support.
expected_evidence describes hypotheses, never observations or changes to evaluation rules.
Local validation and compilation construct the executable protocol and cleanup.
slow_query requires new target-database slow-log entries; qps_drop requires injection QPS
below 0.7 times baseline QPS. Do not claim success before measurement.
"""

SQL_REPAIR_SYSTEM_PROMPT = """SQL validation or EXPLAIN failed. Return the complete corrected
IntentPlan object using the supplied Schema. Only correct the identified SQL field and
its invalid target table/column references when necessary. Never change valid target
references or the target predicate. Preserve node, strategy, load, expected_evidence
and all other SQL fields. Never output executable
protocol fields. Do not change the requested experiment scope or safety constraints."""

PLANNER_JSON_REPAIR_SYSTEM_PROMPT = """Return the complete corrected Intent object using the
supplied Schema: a top-level intents array with node, strategy, target, load, sql,
expected_evidence per root. Preserve requested roots, database and safety limits.
When repair_scope is provided, only correct that identified field and necessary invalid
references; preserve its original plan's other fields, SQL slots and Intent count. JSON only."""

SYSTEM_PROMPT = SINGLE_PASS_SYSTEM_PROMPT


def _is_chaosblade_binary(value: str, configured_path: str) -> bool:
    if not value:
        return False
    return value == configured_path


def _extract_chaosblade_uid(command: list[str]) -> str:
    for idx, part in enumerate(command):
        if part == "--uid" and idx + 1 < len(command):
            return command[idx + 1]
        if part.startswith("--uid="):
            return part.split("=", 1)[1]
    return ""


# ---------------------------------------------------------------------------
# Global Planner
# ---------------------------------------------------------------------------

class GlobalPlanner:
    """
    Planner that sends one complete environment context to the LLM. Local
    code owns normalization, SQL validation, DAG construction and safety.
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._planner_trace: list[PlannerTraceStep] = []
        self.last_plan_payload: dict[str, Any] = {}
        self.last_planner_context: dict[str, Any] = {}
        self.planner_llm_calls = 0
        self.planner_json_repair_attempts = 0
        self.sql_repair_attempts = 0
        self._planner_timings: dict[str, float] = {}
        self._llm_usage: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def inspect(self, request: ExperimentRequest) -> EnvironmentSnapshot:
        """Probe the full environment and return an EnvironmentSnapshot."""
        snapshot_data = tool_registry.probe_full_snapshot(
            config=self.config,
            database=request.target_database,
        )
        schema_data = snapshot_data.get("schema", {})
        schema = SchemaInfo(
            database=request.target_database,
            tables=schema_data,
        )
        return EnvironmentSnapshot(
            database=request.target_database,
            schema=schema,
            table_stats=snapshot_data.get("table_stats", []),
            identifier_whitelist=self._identifier_whitelist(schema_data),
            db_metrics=snapshot_data.get("db_metrics", {}),
            workload_status=snapshot_data.get("workload", {}),
            os_metrics=snapshot_data.get("os_metrics", {}),
            db_version=snapshot_data.get("db_version", ""),
            max_connections=int(snapshot_data.get("db_metrics", {}).get("max_connections", 100)),
            planner_trace=[],
        )

    @_planner_boundary
    def plan(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        memory_items: Optional[List[dict]] = None,
        reflection: Optional[ReflectionResult] = None,
        planning_context: Optional[dict[str, Any]] = None,
        compilation_context: CompilationContext | None = None,
    ) -> tuple[ExecutableTaskDAG, EnvironmentSnapshot, list[PlannerTraceStep]]:
        """
        Run the single-pass planning pipeline to produce an ExecutableTaskDAG.

        Returns (dag, snapshot, planner_trace).
        """
        self._planner_trace = []
        memory_items = memory_items or []
        self.last_plan_payload = {}
        self.last_planner_context = {}
        self.planner_llm_calls = 0
        self.planner_json_repair_attempts = 0
        self.sql_repair_attempts = 0
        self._planner_timings = {}
        self._llm_usage = []
        self._planner_started = time.perf_counter()
        self._compilation_context = compilation_context
        self._active_request = request
        self._validate_request(request)

        # Record the pre-collected planner context.
        self._trace(
            round=1,
            thought="Environment snapshot collected. "
                    f"Schema has {len(snapshot.schema.tables) if snapshot.schema else 0} tables. "
                    f"DB version: {snapshot.db_version}. "
                    f"Max connections: {snapshot.max_connections}.",
            action="collect_planner_context",
            observe=f"Tables: {list(snapshot.schema.tables.keys())[:5] if snapshot.schema else []}",
        )

        try:
            task_specs, dependencies, dag_dict, plan_payload = self._single_pass_planner(
                request=request, snapshot=snapshot, memory_items=memory_items,
                reflection=reflection, planning_context=planning_context,
            )
        except PlannerFallbackError:
            raise
        except Exception:
            raise
        self._stage = "finalize_plan"
        if not dag_dict:
            dag_dict = tool_registry.build_task_dag(task_specs, dependencies)
        dag = self._dict_to_dag(dag_dict)

        self._trace(
            round=len(self._planner_trace) + 1,
            thought=f"DAG built with {len(task_specs)} tasks and {len(dependencies)} edges",
            action="build_task_dag",
            observe=f"Task IDs: {list(dag.tasks.keys())}",
        )

        self.last_plan_payload = {
            "target_path": list(request.target_path),
            "injected_nodes": list(request.injected_nodes),
            "task_specs": task_specs,
            "dependencies": dependencies,
            "dag": dag_dict,
            "reasoning": plan_payload.get("reasoning", ""),
            "safety_check": plan_payload.get("safety_check", {}),
            "success_criteria": plan_payload.get("success_criteria", {}),
            "planner_validation": plan_payload.get("planner_validation", []),
            "planner_mode": "intent_compiler_v1",
            "planner_intent": plan_payload.get("planner_intent"),
            "compilation": plan_payload.get("compilation", {}),
            "planner_llm_calls": self.planner_llm_calls,
            "planner_json_repair_attempts": self.planner_json_repair_attempts,
            "planner_json_repair_max_attempts": 2,
            "sql_repair_attempts": self.sql_repair_attempts,
            "sql_repair_max_attempts": 3,
            "sql_repair_counts": dict(self.sql_repair_counts),
            "intent_schema_version": INTENT_VERSION,
            "compiler_version": COMPILER_VERSION,
            "api_usage": copy.deepcopy(self._llm_usage),
            "planner_timings_sec": dict(self._planner_timings),
        }
        self.last_plan_payload["planner_timings_sec"]["total_sec"] = time.perf_counter() - self._planner_started
        snapshot.planner_trace = self._planner_trace
        return dag, snapshot, self._planner_trace

    def reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        memory_items: list[dict],
    ) -> ReflectionResult:
        """Generate a ReflectionResult from a failed evaluation."""
        from AnomalyGraphAgent.reflection import SelfReflection

        reflector = SelfReflection(self.config)
        return reflector.reflect(evaluation, request, memory_items)

    def _single_pass_planner(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        memory_items: list[dict],
        reflection: ReflectionResult | None,
        planning_context: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[list[str]], dict[str, Any], dict[str, Any]]:
        """Generate once, repair invalid planner JSON, then repair SQL EXPLAIN failures."""
        if not self.config.planner_enabled or not self.config.openai_api_key:
            missing = []
            if not self.config.planner_enabled:
                missing.append("planner_enabled=false")
            if not self.config.openai_api_key:
                missing.append("missing OPENAI_API_KEY")
            reason = "Planner fallback blocked: " + ", ".join(missing)
            self._trace(len(self._planner_trace) + 1, reason, "fallback_blocked")
            raise PlannerFallbackError(
                reason=reason,
                trace=[s.to_dict() for s in self._planner_trace],
                context={
                    "target_path": request.target_path,
                    "injected_nodes": request.injected_nodes,
                    "target_database": request.target_database,
                },
            )

        self._stage = "planner_context"
        context = self._build_planner_context(request, snapshot, memory_items, reflection)
        if planning_context:
            context = self._public_planner_context(copy.deepcopy(planning_context))
        context["request"] = self._public_planner_context(request.to_dict())
        context["duration_limit_sec"] = self._planner_duration_limit(request)
        self._response_format = {"type": "json_schema", "json_schema": {
            "name": "anomaly_intent_plan_v1", "strict": True,
            "schema": build_intent_schema(request.injected_nodes),
        }}
        compilation_context = self._compilation_context or self._make_compilation_context(request)
        if compilation_context.request.to_dict() != request.to_dict():
            raise PlannerFallbackError("CompilationContext request does not match planner request")
        for node in request.injected_nodes:
            from AnomalyGraphAgent.intent import strategies_for
            require_compilable_strategy(node, strategies_for(node)[0].name)
        self.last_planner_context = context
        context["strategy_guidance"] = [
            {"node": node, "strategy": s.name, "requirements": list(s.requirements), "defaults": s.defaults}
            for node in request.injected_nodes for s in strategies_for(node)
        ]
        context["resource_policy"] = self._public_planner_context(compilation_context.resource_profiles)
        history = self._validation_history
        pending = None
        response = self._call_planner_llm(SINGLE_PASS_SYSTEM_PROMPT, json.dumps(context, ensure_ascii=False, default=str), "llm_generate_plan")
        while True:
            self._stage = "intent_schema"
            stage_started = time.perf_counter()
            try:
                intent = self._payload_from_response(response, request, history)
            except (PlannerFallbackError, IntentValidationError) as exc:
                response = self._repair_invalid_intent(request, context, response, exc, pending)
                self._planner_timings["intent_schema_sec"] = self._planner_timings.get("intent_schema_sec", 0.0) + (time.perf_counter() - stage_started)
                continue
            self._planner_timings["intent_schema_sec"] = self._planner_timings.get("intent_schema_sec", 0.0) + (time.perf_counter() - stage_started)
            self._stage = "repair_scope"
            if pending:
                try:
                    guard = guard_sql_repair if pending["kind"] == "sql" else guard_semantic_repair
                    guard(pending["plan"], intent, pending["source"],
                          self._identifier_whitelist(snapshot.schema.tables if snapshot.schema else {}))
                except ValueError as exc:
                    self._fail("repair_scope_violation", str(exc), cause=exc, source=pending["source"])
            self._stage = "intent_semantics"
            stage_started = time.perf_counter()
            try:
                self._validate_intent_targets(intent, request, snapshot)
            except IntentSemanticError as exc:
                if not pending or pending["kind"] != "sql":
                    pending = {"kind": "semantic", "plan": intent, "source": exc.source}
                response = self._repair_invalid_intent(request, context, response, exc, pending)
                self._planner_timings["intent_semantics_sec"] = self._planner_timings.get("intent_semantics_sec", 0.0) + (time.perf_counter() - stage_started)
                continue
            self._planner_timings["intent_semantics_sec"] = self._planner_timings.get("intent_semantics_sec", 0.0) + (time.perf_counter() - stage_started)
            self._stage = "compile_intent"
            stage_started = time.perf_counter()
            try:
                compiled = compile_intent_plan(intent, compilation_context)
                payload = compiled.to_dict()
                self._trace(len(self._planner_trace) + 1, "Intent compiled locally", "compile_intent", f"tasks={len(compiled.task_specs)}")
                self._static_validate_tasks(payload["task_specs"], request, snapshot)
            except Exception as exc:
                self._fail("compiler_error", "Compiled task validation failed: " + str(exc), cause=exc)
            self._planner_timings["compile_intent_sec"] = self._planner_timings.get("compile_intent_sec", 0.0) + (time.perf_counter() - stage_started)
            self._stage = "prepare_payload"
            stage_started = time.perf_counter()
            task_specs, dependencies, dag_dict, normalized = self._prepare_payload(payload, request, snapshot, reflection)
            self._planner_timings["prepare_payload_sec"] = self._planner_timings.get("prepare_payload_sec", 0.0) + (time.perf_counter() - stage_started)
            self._stage = "sql_explain"
            stage_started = time.perf_counter()
            failed = self._first_sql_explain_failure(task_specs, request, snapshot, history)
            self._planner_timings["sql_explain_sec"] = self._planner_timings.get("sql_explain_sec", 0.0) + (time.perf_counter() - stage_started)
            if failed is None:
                self._stage = "safety_check"
                from AnomalyGraphAgent.safety import SafetyChecker
                safety = SafetyChecker(self.config).check(
                    dag_dict, snapshot.db_metrics, snapshot.os_metrics,
                    max_duration_sec=request.max_duration_sec,
                    injection_observe_sec=(request.workload or {}).get("injection_observe_sec") if request.workload.get("enabled") else None,
                    expected_workload=self._planner_workload_context(request),
                )
                self._trace(len(self._planner_trace) + 1, "Local safety check completed", "check_safety", json.dumps(to_jsonable(safety)))
                if not safety.approved:
                    self._fail("safety_rejected", "; ".join(safety.reasons))
                normalized.update(planner_validation=history, safety_check=to_jsonable(safety),
                                  planner_intent=intent, compilation=payload,
                                  intent_schema_version=INTENT_VERSION,
                                  compiler_version=COMPILER_VERSION,
                                  api_usage=copy.deepcopy(self._llm_usage),
                                  planner_timings_sec=dict(self._planner_timings))
                return task_specs, dependencies, dag_dict, normalized
            source = compiled.source_map.get(failed["location"])
            if not source:
                self._fail("compiler_error", "Compiler-generated SQL failed EXPLAIN: " + failed["error"],
                           cause=self._last_explain_cause, source=failed["location"])
            try:
                slot_index, slot_key = sql_slot(intent, source)
                if intent["intents"][slot_index]["sql"][slot_key].strip().removesuffix(";").strip() != failed["sql"].strip():
                    raise ValueError("Compiler SQL does not match its declared Intent source")
            except ValueError as exc:
                self._fail("compiler_error", "Invalid compiler SQL source map", cause=exc, source=source)
            pending = {"kind": "sql", "plan": intent, "source": source}
            attempt = self._reserve_sql_repair(source)
            repair_context = {"repair_type": "sql_explain_failure", "source": source,
                              "attempt": attempt, "max_attempts": 3, "previous_intent": intent,
                              "original_sql": failed["sql"], "error": failed["error"],
                              "planner_context": context}
            response = self._call_planner_llm(SQL_REPAIR_SYSTEM_PROMPT,
                json.dumps(repair_context, ensure_ascii=False, default=str), "llm_repair_sql")

    def _reserve_sql_repair(self, source):
        used = self.sql_repair_counts.get(source, 0)
        if used >= 3:
            self._fail("sql_repair_exhausted", f"SQL EXPLAIN failed after 3 repair attempts: {source}",
                       cause=getattr(self, "_last_explain_cause", None), source=source)
        self.sql_repair_counts[source] = used + 1
        self.sql_repair_attempts += 1
        return used + 1

    def _repair_invalid_intent(self, request, context, response, exc, pending):
        self._validation_history.append({
            "stage": self._stage, "status": "failed", "error": str(exc),
            "raw_output": response.get("text", ""), "payload": response.get("json_payload"),
        })
        if pending:
            candidate = response.get("json_payload")
            if candidate is None:
                try:
                    candidate = json.loads(response.get("text", ""))
                except (ValueError, TypeError):
                    candidate = None
            try:
                guard_partial_repair(pending["plan"], candidate)
            except ValueError as scope_error:
                self._fail("repair_scope_violation", str(scope_error), cause=scope_error, source=pending["source"])
        if self.planner_json_repair_attempts >= 2:
            self._fail("intent_repair_exhausted", str(exc), cause=exc)
        if pending and pending["kind"] == "sql":
            self._reserve_sql_repair(pending["source"])
        self.planner_json_repair_attempts += 1
        return self._repair_planner_json(request, context, response, str(exc),
                                         self.planner_json_repair_attempts, pending=pending)

    def _repair_planner_json(
        self,
        request: ExperimentRequest,
        planner_context: dict[str, Any],
        response: dict[str, Any],
        error: str,
        attempt: int,
        pending: dict | None = None,
    ) -> dict[str, Any]:
        raw_output = response.get("text", "")
        repair_context = {
            "repair_type": "planner_json_schema_failure",
            "attempt": attempt,
            "max_attempts": 2,
            "target_path": request.target_path,
            "injected_nodes": request.injected_nodes,
            "target_database": request.target_database,
            "error": error,
            "raw_output": raw_output,
            "planner_context": planner_context,
            "repair_scope": pending,
            "invalid_payload": response.get("json_payload"),
        }
        return self._call_planner_llm(
            PLANNER_JSON_REPAIR_SYSTEM_PROMPT,
            json.dumps(repair_context, ensure_ascii=False, default=str),
            "llm_repair_planner_json",
        )

    def _call_planner_llm(self, system_prompt: str, user_prompt: str, action: str) -> dict[str, Any]:
        self._stage = action
        started = time.perf_counter()
        self.planner_llm_calls += 1
        self._trace(len(self._planner_trace) + 1, "Planner LLM call", action, f"call={self.planner_llm_calls}")
        try:
            response = tool_registry.llm_generate(
                config=self.config, system_prompt=system_prompt, user_prompt=user_prompt,
                temperature=self.config.planner_temperature, json_mode=True,
                response_format=self._response_format,
            )
        except (tool_registry.LLMTimeoutError, ConnectionError, OSError) as exc:
            self._fail("llm_connection_error", str(exc), cause=exc, llm=True)
        if not isinstance(response, dict):
            self._fail("invalid_llm_response", "LLM client returned a non-object envelope")
        if response.get("error") and response.get("error_type") not in {"invalid_json", "invalid_output_shape", "truncated_response"}:
            self._fail("llm_api_error", f"Planner LLM failed: {response['error']}", llm=True)
        self._planner_timings[action] = self._planner_timings.get(action, 0.0) + (time.perf_counter() - started)
        self._llm_usage.append({
            "action": action,
            "call": self.planner_llm_calls,
            "usage": copy.deepcopy(response.get("usage")),
            "usage_status": response.get("usage_status", "unavailable"),
            "request_id": response.get("request_id"),
            "response_id": response.get("response_id"),
            "finish_reason": response.get("finish_reason"),
        })
        self._validation_history.append({"stage": action, "status": "response",
                                         "response": copy.deepcopy(response)})
        return response

    def _fail(self, error_type, reason, *, cause=None, source=None, llm=False):
        reason = tool_registry._redact_llm_result(reason, self.config)
        context = {
            "stage": self._stage, "error_type": error_type, "source": source,
            "planner_llm_calls": self.planner_llm_calls,
            "planner_json_repair_attempts": self.planner_json_repair_attempts,
            "sql_repair_attempts": self.sql_repair_attempts,
            "sql_repair_counts": dict(self.sql_repair_counts),
            "planner_timings_sec": {**self._planner_timings, "total_sec": time.perf_counter() - getattr(self, "_planner_started", time.perf_counter())},
            "validation_history": self._validation_history,
        }
        self._validation_history.append({"stage": self._stage, "status": "terminated",
                                         "error_type": error_type, "error": reason, "source": source})
        self._trace(len(self._planner_trace) + 1, reason, "planner_validation_failed")
        error = PlannerLLMError if llm else PlannerFallbackError
        raise error(reason, trace=[s.to_dict() for s in self._planner_trace],
                    context=tool_registry._redact_llm_result(context, self.config)) from (cause or RuntimeError(reason))

    def _payload_from_response(self, response: dict[str, Any], request: ExperimentRequest, history: list[dict[str, Any]]) -> dict[str, Any]:
        if response.get("error"):
            raise PlannerFallbackError(response["error"])
        payload = response.get("json_payload")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(response.get("text", ""))
            except (TypeError, json.JSONDecodeError) as exc:
                raw = response.get("text", "")
                history.append({"status": "invalid_json", "error": str(exc), "raw_output": raw})
                raise PlannerFallbackError(
                    f"Planner JSON parse failed: {exc}",
                    trace=[s.to_dict() for s in self._planner_trace],
                    context={"raw_output": raw, "validation_history": history},
                ) from exc
        if not isinstance(payload, dict):
            raise PlannerFallbackError(
                "Planner JSON must be an Intent object",
                    trace=[s.to_dict() for s in self._planner_trace],
                context={"raw_output": response.get("text", ""), "validation_history": history},
            )
        intent = validate_intent_plan(payload, request.injected_nodes)
        intent["intents"].sort(key=lambda item: request.injected_nodes.index(item["node"]))
        return intent

    @staticmethod
    def _identifier_whitelist(tables: dict[str, dict]) -> dict[str, Any]:
        return {str(name): [str(c.get("name", c)) if isinstance(c, dict) else str(c) for c in (info.get("columns") or [])]
                for name, info in (tables or {}).items() if isinstance(info, dict)}

    def _build_planner_context(self, request, snapshot, memory_items, reflection) -> dict[str, Any]:
        return self._public_planner_context({
            "request": request.to_dict(),
            "environment": {
                "db_version": snapshot.db_version, "schema": snapshot.schema.tables if snapshot.schema else {},
                "table_stats": snapshot.table_stats, "db_metrics": snapshot.db_metrics, "os_metrics": snapshot.os_metrics,
                "identifier_whitelist": self._identifier_whitelist(snapshot.schema.tables if snapshot.schema else {}),
            },
            "workload": {"config": self._planner_workload_context(request), "baseline": snapshot.workload_status},
            "memory": memory_items or [], "reflection": to_jsonable(reflection) if reflection else {},
            "runtime_paths": {"benchbase_java_bin": self.config.benchbase_java_bin, "benchbase_jar_path": self.config.benchbase_jar_path, "chaosblade_path": self.config.chaosblade_path},
            "duration_limit_sec": self._planner_duration_limit(request),
        })

    def _prepare_payload(self, payload, request, snapshot, reflection):
        raw = payload.get("task_specs") or []
        self._validate_planner_payload_shape(raw)
        specs = copy.deepcopy(raw)
        self._normalize_transaction_scripts(specs)
        limit = self._planner_duration_limit(request)
        if limit is not None:
            clamps = self._clamp_planner_task_durations(specs, limit, "retry" if reflection else "initial")
            if clamps:
                self._trace(len(self._planner_trace) + 1, "Planner durations clamped locally", "clamp_planner_duration", json.dumps(clamps, ensure_ascii=False))
        specs = self._filter_task_specs(specs, request)
        self._static_validate_tasks(specs, request, snapshot)
        deps = self._filter_dependencies(payload.get("dependencies", self._build_dependencies(request.target_path, specs)), specs)
        dag = tool_registry.build_task_dag(specs, deps)
        from AnomalyGraphAgent.safety import estimate_dag_required_sec
        required, reasons = estimate_dag_required_sec(dag)
        from AnomalyGraphAgent.dag import topological_order
        from AnomalyGraphAgent.types import TaskDAGEdge
        edges = [TaskDAGEdge(**edge) for edge in dag["edges"]]
        ends = {}
        for task_id in topological_order(dag["tasks"], edges):
            elapsed, _ = estimate_dag_required_sec({"tasks": {task_id: dag["tasks"][task_id]}})
            predecessors = [ends[e.source] for e in edges if e.target == task_id]
            ends[task_id] = max([dag["schedule"].get(task_id, 0), *predecessors]) + elapsed
        required = max([required, *ends.values()])
        if reasons or (limit is not None and required > limit):
            raise ValueError("Timing validation failed: " + "; ".join(reasons + [f"DAG required duration {required}s; limit {limit}s"]))
        result = dict(payload); result["task_specs"] = specs; result["dependencies"] = deps; result["dag"] = dag
        return specs, deps, dag, result

    @staticmethod
    def _validate_planner_payload_shape(task_specs: Any) -> None:
        """Reject old or incomplete TaskSpec shapes before any local normalization."""
        if not isinstance(task_specs, list) or not task_specs:
            raise ValueError("planner JSON task_specs must be a non-empty array")
        required = {
            "task_id", "task_type", "actions", "expected_metrics", "success_criteria",
            "start_after_sec", "dependencies", "cleanup_actions", "risk_assessment", "metadata",
        }
        legacy = {"anomaly", "node", "injection", "mode", "action_type", "dag", "safety_check"}
        for index, spec in enumerate(task_specs):
            if not isinstance(spec, dict):
                raise ValueError(f"task_specs[{index}] must be an object")
            missing = sorted(required - set(spec))
            if missing:
                raise ValueError(f"task_specs[{index}] missing required fields: {', '.join(missing)}")
            old = sorted(legacy & set(spec))
            if old:
                raise ValueError(f"task_specs[{index}] contains legacy fields: {', '.join(old)}")
            if not str(spec.get("task_id") or "").strip():
                raise ValueError(f"task_specs[{index}].task_id is required")
            if not isinstance(spec.get("actions"), list) or not spec["actions"]:
                raise ValueError(f"task_specs[{index}].actions must be a non-empty array")
            if not isinstance(spec.get("metadata"), dict) or not str(spec["metadata"].get("root_cause") or "").strip():
                raise ValueError(f"task_specs[{index}].metadata.root_cause is required")
            for action_index, action in enumerate(spec["actions"]):
                if not isinstance(action, dict):
                    raise ValueError(f"task_specs[{index}].actions[{action_index}] must be an object")
                old_action = sorted(legacy & set(action))
                if old_action:
                    raise ValueError(
                        f"task_specs[{index}].actions[{action_index}] contains legacy fields: {', '.join(old_action)}"
                    )
                if not str(action.get("kind") or "").strip():
                    raise ValueError(f"task_specs[{index}].actions[{action_index}].kind is required")

    @staticmethod
    def _normalize_transaction_scripts(specs):
        for spec in specs:
            for action in spec.get("actions") or []:
                if action.get("kind") != "raw_transaction_script":
                    continue
                scripts = action.get("scripts")
                if not isinstance(scripts, list) or not scripts:
                    raise ValueError("raw_transaction_script requires non-empty scripts")
                for script_index, script in enumerate(scripts):
                    if isinstance(script, str):
                        script = {"sql": script}
                        scripts[script_index] = script
                    if not isinstance(script, dict):
                        raise ValueError("transaction script must be an object")
                    if "steps" not in script:
                        values = script.pop("sqls", None)
                        if values is None and script.get("sql") is not None:
                            values = [script.pop("sql")]
                        if values is None:
                            values = []
                        if isinstance(values, str): values = [values]
                        script["steps"] = [{"sql": v} if isinstance(v, str) else v for v in values]
                if any(not s.get("steps") for s in scripts if isinstance(s, dict)):
                    raise ValueError("raw_transaction_script contains empty steps")

    @staticmethod
    def _task_sqls(specs):
        for ti, spec in enumerate(specs):
            for ai, action in enumerate(spec.get("actions", [])):
                base = f"task_specs[{ti}].actions[{ai}]"
                if action.get("kind") == "raw_sql_workload":
                    yield base + ".sql", action["sql"], False
                elif action.get("kind") == "raw_transaction_script":
                    for si, script in enumerate(action["scripts"]):
                        for pi, step in enumerate(script["steps"]):
                            if step.get("sql"):
                                yield f"{base}.scripts[{si}].steps[{pi}].sql", step["sql"], True

    def _static_validate_tasks(self, specs, request, snapshot):
        from AnomalyGraphAgent.safety import _DANGEROUS_SQL_PATTERNS, _PROD_DB_PATTERNS
        whitelist = self._identifier_whitelist(snapshot.schema.tables if snapshot.schema else {})
        if any(pattern.search(request.target_database) for pattern in _PROD_DB_PATTERNS):
            raise SQLValidationError("Production database targets are prohibited")
        for spec in specs:
            self._validate_direct_task_spec(spec, request)
            for action in spec["actions"]:
                if action.get("database") != request.target_database:
                    raise SQLValidationError("Action database does not match request")
                for script in action.get("scripts", []):
                    if script.get("database", request.target_database) != request.target_database:
                        raise SQLValidationError("Script database does not match request")
        for location, sql, controls in self._task_sqls(specs):
            if any(pattern.search(sql) for pattern in _DANGEROUS_SQL_PATTERNS):
                raise SQLValidationError(f"{location}: SQL rejected by safety policy")
            validate_sql(sql, request.target_database, whitelist, controls=controls)

    def _first_sql_explain_failure(self, specs, request, snapshot, history):
        import pymysql
        whitelist = self._identifier_whitelist(snapshot.schema.tables if snapshot.schema else {})
        for location, sql, controls in self._task_sqls(specs):
            validation = validate_sql(sql, request.target_database, whitelist, controls=controls)
            if not validation["explain"]:
                history.append({"location": location, "sql": sql, "status": "passed", **validation})
                continue
            try:
                result = tool_registry.explain_sql(self.config, request.target_database, sql)
            except (ConnectionError, OSError) as exc:
                self._fail("database_connection_error", str(exc), cause=exc, source=location)
            except pymysql.MySQLError as exc:
                if exc.args and exc.args[0] in {1044, 1045, 1129, 1130, 2002, 2003, 2006, 2013}:
                    self._fail("database_connection_error", str(exc), cause=exc, source=location)
                result = {"plan": [{"error": str(exc)}]}
                self._last_explain_cause = exc
            if not isinstance(result, dict) or result.get("error") or not isinstance(result.get("plan"), list):
                self._fail("invalid_probe_response", "EXPLAIN response is unavailable or malformed", source=location)
            for row in result["plan"]:
                if not isinstance(row, dict):
                    self._fail("invalid_probe_response", "EXPLAIN rows must be objects", source=location)
                if row.get("error_type") == "database_error":
                    self._fail("database_connection_error", str(row.get("error")), source=location)
                if row.get("error"):
                    error = str(row["error"])
                    self._last_explain_cause = ValueError(error)
                    failed = {"location": location, "sql": sql, "status": "failed", "error": error}
                    history.append(failed)
                    return failed
            history.append({"location": location, "sql": sql, "status": "passed", "plan": result["plan"], **validation})
        return None

    def _validate_intent_targets(self, plan, request, snapshot):
        whitelist = self._identifier_whitelist(snapshot.schema.tables if snapshot.schema else {})
        for index, intent in enumerate(plan["intents"]):
            base = f"intents[{index}]"
            target = intent["target"] or {}
            tables = target.get("tables") or ([target["table"]] if target.get("table") else [])
            for key, sql in (intent.get("sql") or {}).items():
                try:
                    validation = validate_sql(sql, request.target_database, whitelist)
                    if not set(validation["tables"]).issubset(tables):
                        raise SQLValidationError(f"{key}: SQL references tables outside Intent target")
                except SQLValidationError as exc:
                    raise IntentSemanticError(f"{base}.sql.{key}", str(exc)) from exc
            for table in tables:
                if table not in whitelist:
                    field = "tables" if "tables" in target else "table"
                    raise IntentSemanticError(f"{base}.target.{field}", f"Target table not in identifier whitelist: {table}")
            columns = {col.lower() for table in tables for col in whitelist[table]}
            for column in target.get("columns", []):
                if column.lower() not in columns:
                    raise IntentSemanticError(f"{base}.target.columns", f"Target column not in identifier whitelist: {column}")
            if target.get("predicate"):
                try:
                    validate_sql(f"SELECT * FROM `{target['table']}` WHERE {target['predicate']}", request.target_database, whitelist)
                except SQLValidationError as exc:
                    # Predicates define the hotspot and cannot be silently relocated.
                    self._fail("invalid_target_predicate", str(exc), cause=exc, source=base + ".target.predicate")
            if intent["node"] in {"hot_update", "long_tx"}:
                from AnomalyGraphAgent.intent_compiler import _hot_sql, IntentCompilationError
                try:
                    _hot_sql(intent)
                except IntentCompilationError as exc:
                    raise IntentSemanticError(base + ".sql.waiter_sql", str(exc)) from exc

    def _public_planner_context(self, value):
        blocked = {"runtime_paths", "config_path", "jar_path", "java_bin", "command", "cleanup_command",
                   "task_specs", "dag", "actions", "cleanup_actions", "task_id", "uid",
                   "password", "mysql_password", "openai_api_key", "authorization", "compilation_context", "path"}
        if isinstance(value, dict):
            return {k: self._public_planner_context(v) for k, v in value.items() if k.lower() not in blocked}
        if isinstance(value, list):
            return [self._public_planner_context(v) for v in value]
        return tool_registry._redact_llm_result(value, self.config)

    def _make_compilation_context(self, request):
        from pathlib import Path
        import xml.etree.ElementTree as ET
        kwargs = {}
        if "traffic_surge" in request.injected_nodes:
            workload = self._planner_workload_context(request)
            path = workload.get("config_path")
            if not path:
                raise ValueError("Traffic surge requires workload.config_path")
            root = ET.parse(path).getroot()
            names = tuple(n.text.strip() for n in root.findall(".//transactiontypes/transactiontype/name") if n.text)
            works = root.findall(".//works/work")
            if not names or len(works) != 1:
                raise ValueError("Traffic surge requires one unambiguous workload phase and named transactions")
            weights = [float(x.strip()) for x in works[0].findtext("weights", "").split(",") if x.strip()]
            if len(weights) != len(names):
                raise ValueError("BenchBase transaction weights do not match transaction types")
            kwargs.update(transaction_types=names, benchbase_profile={
                "benchmark": workload["benchmark"], "database": request.target_database,
                "config_path": str(Path(path).absolute()), "terminals": workload.get("terminals", 4),
                "duration_sec": self._planner_duration_limit(request),
                "rate": workload.get("rate") or works[0].findtext("rate"),
                "transaction_mix": dict(zip(names, weights)), "mix_template": "background",
                "rationale": "Inherit the configured background transaction mix",
            })
        resource_profiles = {
            "resource_cpu": {k: {"percent": min(v, self.config.max_cpu_usage)} for k, v in (("low", 40), ("medium", 65), ("high", 90))},
            "resource_memory": {k: {"percent": v} for k, v in (("low", 20), ("medium", 40), ("high", 60))},
        }
        for node in ("resource_io", "disk_full_or_pressure"):
            resource_profiles[node] = {k: {"size": size, "path": "/tmp/dbmags"} for k, size in (("low", "32M"), ("medium", "64M"), ("high", "128M"))}
        return CompilationContext(request=request, config=self.config, run_id=uuid.uuid4().hex,
            resource_profiles=resource_profiles, mysqldump_path=getattr(self.config, "mysqldump_path", ""),
            mysql_defaults_file=getattr(self.config, "mysql_defaults_file", ""), **kwargs)

    def _planner_validation_failure(self, request, reason, history):
        self._trace(len(self._planner_trace) + 1, reason, "planner_validation_failed")
        raise PlannerFallbackError(
            reason=reason,
            trace=[s.to_dict() for s in self._planner_trace],
            context={"target_path": request.target_path, "injected_nodes": request.injected_nodes,
                     "validation_history": history},
        )

    @staticmethod
    def _normalize_task_spec_root_aliases(task_specs: list[dict[str, Any]], request: ExperimentRequest) -> None:
        allowed = set(request.injected_nodes)
        aliases = {
            "redo_log_flush_stall": "redo_log_pressure",
            "commit_latency_up": "redo_log_pressure",
            "metadata_lock_wait": "metadata_lock",
            "connection_pressure": "connection_storm",
            "network_stall": "network_latency",
            "disk_saturation": "disk_full_or_pressure",
            "deadlock_detected": "deadlock_storm",
            "temp_table_spill": "large_temp_table",
            "lock_contention": "table_lock",
        }
        for spec in task_specs:
            metadata = spec.get("metadata")
            if not isinstance(metadata, dict):
                continue
            root = str(metadata.get("root_cause") or "")
            mapped = aliases.get(root)
            if mapped in allowed:
                metadata["root_cause"] = mapped

    # -------------------------------------------------------------------------
    # DAG helpers
    # -------------------------------------------------------------------------

    def _build_dependencies(
        self,
        target_path: list[str],
        task_specs: list[dict[str, Any]],
    ) -> list[list[str]]:
        """Infer dependencies from the propagation path order."""
        task_by_node: dict[str, str] = {}
        for spec in task_specs:
            meta = spec.get("metadata", {})
            root = meta.get("root_cause", "")
            if root:
                task_by_node[root] = spec.get("task_id", "")

        dependencies: list[list[str]] = []
        injected_order = [node_id for node_id in target_path if node_id in task_by_node]
        for src, dst in zip(injected_order, injected_order[1:]):
            dependencies.append([task_by_node[src], task_by_node[dst]])
        return dependencies

    def _dict_to_dag(self, dag_dict: dict) -> ExecutableTaskDAG:
        tasks = {
            tid: TaskSpec(**{k: v for k, v in t.items() if k in TaskSpec.__dataclass_fields__})
            for tid, t in dag_dict.get("tasks", {}).items()
        }
        edges = [TaskDAGEdge(**e) for e in dag_dict.get("edges", [])]
        return ExecutableTaskDAG(
            tasks=tasks,
            edges=edges,
            schedule=dag_dict.get("schedule", {}),
        )

    # -------------------------------------------------------------------------
    # Planner tracing
    # -------------------------------------------------------------------------

    def _trace(self, round: int, thought: str, action: str, observe: str = "") -> None:
        step = PlannerTraceStep(
            round=round,
            thought=thought,
            action=action,
            observe=observe,
            decision="",
        )
        self._planner_trace.append(step)

    def _validate_request(self, request: ExperimentRequest) -> None:
        if not request.target_path:
            raise ValueError("target_path is required")
        if not request.injected_nodes:
            raise ValueError("injected_nodes is required")
        for node_id in request.target_path:
            if ANOMALY_GRAPH.node(node_id) is None:
                raise ValueError(f"target_path contains unknown node: {node_id}")
        edges = {(edge.src, edge.dst) for edge in ANOMALY_GRAPH.edges}
        for src, dst in zip(request.target_path, request.target_path[1:]):
            if (src, dst) not in edges:
                raise ValueError(f"target_path contains invalid edge: {src} -> {dst}")
        path_set = set(request.target_path)
        for node_id in request.injected_nodes:
            node = ANOMALY_GRAPH.node(node_id)
            if node is None:
                raise ValueError(f"injected_nodes contains unknown node: {node_id}")
            if node_id not in path_set:
                raise ValueError(f"injected node '{node_id}' is not in target_path")
            if not node.injectable:
                raise ValueError(f"injected node '{node_id}' is not injectable")

    def _filter_task_specs(self, task_specs: list[dict[str, Any]], request: ExperimentRequest) -> list[dict[str, Any]]:
        allowed = set(request.injected_nodes)
        filtered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for spec in task_specs:
            if not isinstance(spec, dict):
                continue
            root = str((spec.get("metadata") or {}).get("root_cause", ""))
            if root not in allowed:
                self._trace(
                    round=len(self._planner_trace) + 1,
                    thought=f"Dropped TaskSpec for non-requested injected node: {root or '<missing>'}",
                    action="filter_task_spec",
                    observe=json.dumps({"task_id": spec.get("task_id"), "root_cause": root}, ensure_ascii=False),
                )
                continue
            if root in seen:
                raise ValueError(f"multiple TaskSpecs generated for injected node '{root}'")
            self._validate_direct_task_spec(spec, request)
            seen.add(root)
            filtered.append(spec)
        missing = allowed - seen
        if missing:
            raise ValueError(f"missing TaskSpecs for injected nodes: {', '.join(sorted(missing))}")
        return filtered

    def _validate_direct_task_spec(self, spec: dict[str, Any], request: ExperimentRequest) -> None:
        required = (
            "task_id", "task_type", "actions", "expected_metrics", "success_criteria",
            "start_after_sec", "dependencies", "cleanup_actions", "risk_assessment", "metadata",
        )
        missing = [name for name in required if name not in spec]
        if missing:
            raise ValueError(f"TaskSpec missing required fields: {', '.join(missing)}")
        legacy = sorted(set(spec) & {"anomaly", "node", "injection", "mode", "action_type", "dag", "safety_check"})
        if legacy:
            raise ValueError(f"TaskSpec contains legacy fields: {', '.join(legacy)}")
        metadata = spec.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("TaskSpec metadata must be an object")
        root = str(metadata.get("root_cause", ""))
        if not root:
            raise ValueError("TaskSpec metadata.root_cause is required")
        if root not in set(request.injected_nodes):
            raise ValueError(f"TaskSpec root_cause '{root}' is not in injected_nodes")
        actions = spec.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"TaskSpec '{spec.get('task_id')}' must contain at least one action")
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(f"TaskSpec '{spec.get('task_id')}' contains a non-object action")
            legacy_action = sorted(set(action) & {"action_type", "anomaly", "node", "injection", "mode"})
            if legacy_action:
                raise ValueError(f"Action contains legacy fields: {', '.join(legacy_action)}")
            kind = str(action.get("kind", ""))
            if kind not in ALLOWED_PLANNER_ACTION_KINDS:
                raise ValueError(f"TaskSpec '{spec.get('task_id')}' uses unsupported action kind: {kind}")
            if kind in {"raw_command", "logical_backup_command", "benchbase_burst_command"}:
                command = action.get("command")
                if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
                    raise ValueError(f"Action kind '{kind}' requires command as argv string array")
            if kind == "raw_sql_workload" and not str(action.get("sql", "")).strip():
                raise ValueError("raw_sql_workload requires sql")
            if kind == "raw_transaction_script":
                scripts = action.get("scripts")
                if not isinstance(scripts, list) or not scripts:
                    raise ValueError("raw_transaction_script requires non-empty scripts")
            if kind == "benchbase_burst":
                tool_registry.validate_traffic_surge_profile(action.get("profile") or {})
            duration_value = action.get("duration_sec")
            if kind == "benchbase_burst" and duration_value is None:
                duration_value = (action.get("profile") or {}).get("duration_sec")
            if duration_value is None:
                raise ValueError(f"Action kind '{kind}' requires duration_sec")
            try:
                if float(duration_value) <= 0:
                    raise ValueError(f"Action kind '{kind}' duration_sec must be positive")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Action kind '{kind}' duration_sec is invalid") from exc
        if root in RESOURCE_ROOT_CAUSES:
            self._validate_resource_chaosblade_task(spec)
        workload = request.workload or {}
        if workload.get("enabled"):
            for action in actions:
                kind = action.get("kind")
                if kind not in {"benchbase_burst", "benchbase_burst_command"}:
                    continue
                expected_benchmark = str(workload.get("benchmark") or "").lower()
                expected_database = str(workload.get("database") or request.target_database)
                if kind == "benchbase_burst":
                    profile = tool_registry.validate_traffic_surge_profile(action.get("profile") or {})
                    benchmark = profile.get("benchmark")
                    database = profile.get("database")
                else:
                    benchmark = str(action.get("benchmark") or "").lower()
                    database = str(action.get("database") or "")
                if expected_benchmark and benchmark and benchmark != expected_benchmark:
                    raise ValueError(
                        f"benchbase burst benchmark must match background workload ({benchmark} != {expected_benchmark})"
                    )
                if expected_database and database and database != expected_database:
                    raise ValueError(
                        f"benchbase burst database must match background workload ({database} != {expected_database})"
                    )

    def _validate_resource_chaosblade_task(self, spec: dict[str, Any]) -> None:
        actions = spec.get("actions") or []
        for action in actions:
            kind = str(action.get("kind", ""))
            if kind != "raw_command":
                raise ValueError("resource TaskSpec must use raw_command with a ChaosBlade command")
            command = action.get("command")
            cleanup = action.get("cleanup_command")
            if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
                raise ValueError("resource raw_command requires command as argv string array")
            if not isinstance(cleanup, list) or not all(isinstance(part, str) for part in cleanup):
                raise ValueError("resource raw_command requires cleanup_command as argv string array")
            if not _is_chaosblade_binary(command[0], self.config.chaosblade_path):
                raise ValueError("resource raw_command must invoke RuntimeConfig.chaosblade_path, not bare 'blade'")
            if "create" not in command:
                raise ValueError("resource ChaosBlade command must create an experiment")
            self._validate_darwin_chaosblade_command(command)
            uid = _extract_chaosblade_uid(command)
            if not uid:
                raise ValueError("resource ChaosBlade command must include a unique --uid")
            if not _is_chaosblade_binary(cleanup[0], self.config.chaosblade_path):
                raise ValueError("resource cleanup_command must invoke RuntimeConfig.chaosblade_path, not bare 'blade'")
            if "destroy" not in cleanup:
                raise ValueError("resource cleanup_command must destroy the ChaosBlade experiment")
            if uid not in cleanup:
                raise ValueError("resource cleanup_command must contain the same ChaosBlade uid")
            try:
                duration = float(action.get("duration_sec", 0) or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0:
                raise ValueError("resource raw_command duration_sec must be greater than 0")

    @staticmethod
    def _validate_darwin_chaosblade_command(command: list[str]) -> None:
        if len(command) < 4:
            raise ValueError("resource ChaosBlade command must include a Darwin-supported subcommand")
        forbidden = {"--duration", "--process-name", "--read-bps", "--write-bps"}
        used_forbidden = sorted(part for part in command if part in forbidden)
        if used_forbidden:
            raise ValueError(f"resource ChaosBlade command uses unsupported flags: {used_forbidden}")
        target = command[2]
        subcommand = command[3]
        if target == "cpu" and subcommand != "fullload":
            raise ValueError("resource_cpu must use 'cpu fullload'")
        if target == "mem":
            if subcommand != "load":
                raise ValueError("resource_memory must use 'mem load'")
            if "--mode" not in command:
                raise ValueError("resource_memory must include --mode")
        if target == "disk":
            if subcommand != "burn":
                raise ValueError("resource_io must use 'disk burn'")
            if "--read" not in command or "--write" not in command:
                raise ValueError("resource_io must include --read and --write")
        if target in {"cpu", "mem", "disk"} and "--timeout" not in command:
            raise ValueError("resource ChaosBlade command must include --timeout")

    @staticmethod
    def _filter_dependencies(dependencies: list[list[str]], task_specs: list[dict[str, Any]]) -> list[list[str]]:
        task_ids = {str(spec.get("task_id")) for spec in task_specs}
        filtered: list[list[str]] = []
        for dep in dependencies or []:
            if len(dep) != 2:
                continue
            src, dst = str(dep[0]), str(dep[1])
            if src in task_ids and dst in task_ids:
                filtered.append([src, dst])
        return filtered

    # -------------------------------------------------------------------------
    # LLM context builder
    # -------------------------------------------------------------------------

    def _build_llm_context(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        memory_data: list[dict],
        reflection: ReflectionResult | None,
    ) -> str:
        """Build the user prompt context for the LLM."""
        def truncate_text(value: Any, limit: int = 1000) -> str:
            text = str(value or "")
            return text if len(text) <= limit else text[:limit] + "...[truncated]"

        def compact_reflection(value: ReflectionResult | None) -> str:
            if not value:
                return "No reflection for this round."
            data = to_jsonable(value)
            compact = {
                "failure_reason": truncate_text(data.get("failure_reason"), 1200),
                "suggested_changes": [
                    truncate_text(item, 500)
                    for item in (data.get("suggested_changes") or [])[:5]
                ],
                "task_parameter_updates": data.get("task_parameter_updates") or {},
                "risk_warning": truncate_text(data.get("risk_warning"), 800),
            }
            return json.dumps(compact, ensure_ascii=False, default=str)

        def compact_workload_status(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            compact: dict[str, Any] = {}
            for key in ("phase", "sample_count", "running", "pid", "exit_code", "summary_flat"):
                if key in value:
                    compact[key] = value.get(key)
            summary = value.get("summary")
            if isinstance(summary, dict):
                compact_summary: dict[str, Any] = {}
                for key in ("qps", "tps", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
                    if key in summary:
                        compact_summary[key] = summary.get(key)
                db_metrics = summary.get("db_metrics")
                if isinstance(db_metrics, dict):
                    metric_keys = (
                        "Threads_connected",
                        "Threads_running",
                        "Slow_queries",
                        "Innodb_row_lock_waits",
                        "Innodb_row_lock_time",
                        "Questions",
                        "Com_commit",
                        "Com_rollback",
                    )
                    compact_summary["db_metrics"] = {
                        key: db_metrics.get(key)
                        for key in metric_keys
                        if key in db_metrics
                    }
                if compact_summary:
                    compact["summary"] = compact_summary
            elif summary is not None:
                compact["summary"] = summary
            if not compact:
                for key in list(value.keys())[:10]:
                    if key not in {"samples", "stdout_tail", "stderr_tail"}:
                        compact[key] = value.get(key)
            return compact

        schema_summary = ""
        if snapshot.schema and snapshot.schema.tables:
            tables = snapshot.schema.tables
            schema_summary = "Tables: " + ", ".join(
                f"{name}({len(info.get('columns', []))} cols)"
                for name, info in list(tables.items())[:10]
            )

        metrics_summary = ""
        dbm = snapshot.db_metrics
        if dbm:
            metrics_summary = (
                f"Threads_connected={dbm.get('Threads_connected', '?')}, "
                f"Threads_running={dbm.get('Threads_running', '?')}, "
                f"Slow_queries={dbm.get('Slow_queries', '?')}, "
                f"Innodb_row_lock_waits={dbm.get('Innodb_row_lock_waits', '?')}"
            )

        memory_summary = ""
        if memory_data:
            recent = memory_data[:5]
            memory_summary = "Prior attempts:\n" + "\n".join(
                f"  - outcome: {m.get('outcome', '')}, success: {m.get('success', False)}, "
                f"node_hit_ratio: {m.get('node_hit_ratio', 0)}"
                for m in recent
            )

        graph_edges = "\n".join(
            f"- {edge.src} -> {edge.dst} ({edge.relation.value})"
            for edge in ANOMALY_GRAPH.edges
            if edge.src in request.target_path or edge.dst in request.target_path
        )
        reflection_summary = compact_reflection(reflection)
        workload_status_summary = compact_workload_status(snapshot.workload_status)
        planning_round = "retry" if reflection is not None else "initial"
        planner_duration_limit = self._planner_duration_limit(request)

        workload_context = self._planner_workload_context(request)
        runtime_paths = {
            "benchbase_java_bin": self.config.benchbase_java_bin,
            "benchbase_jar_path": self.config.benchbase_jar_path,
            "chaosblade_path": self.config.chaosblade_path,
        }

        context = f"""## User Request
target_anomaly: {request.target_anomaly}
target_database: {request.target_database}
dba_description: {request.dba_description or '(none)'}
max_duration_sec: {request.max_duration_sec}
target_path: {json.dumps(request.target_path, ensure_ascii=False)}
injected_nodes: {json.dumps(request.injected_nodes, ensure_ascii=False)}

## Environment Snapshot
{schema_summary}

DB Metrics: {metrics_summary}

Runtime Workload Status:
{json.dumps(workload_status_summary, ensure_ascii=False, default=str)}

Background Workload Config:
{json.dumps(workload_context, ensure_ascii=False, default=str)}

Trusted Runtime Executable Paths (copy these exact absolute strings into commands):
{json.dumps(runtime_paths, ensure_ascii=False, default=str)}

## User-Specified Propagation Path
{' -> '.join(request.target_path)}

## Relevant Graph Edges
{graph_edges}

## Memory (recent prior attempts)
{memory_summary if memory_summary else 'No prior memory.'}

## Reflection Context For Retry
{reflection_summary}

## Planner Duration Hard Constraint
planning_round: {planning_round}
duration_limit_sec: {planner_duration_limit if planner_duration_limit is not None else '(not applied)'}
For every planning round, every action.duration_sec and every benchbase_burst.profile.duration_sec
MUST be positive and no greater than duration_limit_sec. Increase concurrency, terminals,
rate, resource intensity, hot-key concentration, or transaction mix instead of duration.
The complete scheduled DAG must still fit inside duration_limit_sec.

## Your Task
Use tool calls to generate task_specs only for injected_nodes.
Do not add, remove, or reorder target_path or injected_nodes.
Return the JSON format described in your system prompt.
"""
        return context

    @staticmethod
    def _planner_duration_limit(request: ExperimentRequest) -> float | None:
        limit = float(request.max_duration_sec)
        workload = request.workload or {}
        if workload.get("enabled"):
            observe_sec = float(workload.get("injection_observe_sec", limit) or limit)
            limit = min(limit, observe_sec)
        return limit if limit > 0 else None

    @staticmethod
    def _clamp_planner_task_durations(
        task_specs: list[dict[str, Any]],
        duration_limit_sec: float,
        planning_round: str,
    ) -> list[dict[str, Any]]:
        clamps: list[dict[str, Any]] = []
        final_value: int | float = (
            int(duration_limit_sec)
            if float(duration_limit_sec).is_integer()
            else duration_limit_sec
        )

        def clamp_value(task_id: str, target: dict[str, Any], field_path: str) -> None:
            if "duration_sec" not in target:
                return
            original = target.get("duration_sec")
            if isinstance(original, bool):
                return
            try:
                numeric = float(original)
            except (TypeError, ValueError):
                return
            if numeric <= 0 or numeric <= duration_limit_sec:
                return
            target["duration_sec"] = final_value
            clamps.append({
                "task_id": task_id,
                "field": field_path,
                "planning_round": planning_round,
                "original_duration_sec": original,
                "final_duration_sec": final_value,
            })

        for index, spec in enumerate(task_specs):
            task_id = str(spec.get("task_id") or f"task_{index}")
            task_clamps_before = len(clamps)
            for action_index, action in enumerate(spec.get("actions") or []):
                if not isinstance(action, dict):
                    continue
                clamp_value(task_id, action, f"actions[{action_index}].duration_sec")
                profile = action.get("profile")
                if action.get("kind") == "benchbase_burst" and isinstance(profile, dict):
                    clamp_value(
                        task_id,
                        profile,
                        f"actions[{action_index}].profile.duration_sec",
                    )
            if len(clamps) > task_clamps_before:
                metadata = spec.get("metadata")
                if isinstance(metadata, dict):
                    metadata["planner_duration_limit_sec"] = final_value
                    metadata["planner_duration_clamps"] = [
                        item for item in clamps[task_clamps_before:]
                    ]
        return clamps

    def _planner_workload_context(self, request: ExperimentRequest) -> dict[str, Any]:
        from AnomalyGraphAgent.config import resolve_runtime_path

        workload = dict(request.workload or {})
        workload["jar_path"] = self.config.benchbase_jar_path
        workload["java_bin"] = self.config.benchbase_java_bin
        config_path = str(workload.get("config_path") or "")
        if config_path:
            workload["config_path"] = resolve_runtime_path(config_path)
        return workload
