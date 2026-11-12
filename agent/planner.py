"""Single global planner using native chat tool-calling."""

from __future__ import annotations

import json
from typing import Any, List, Optional

from agent.config import RuntimeConfig
from agent.graph import ANOMALY_GRAPH
from agent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExperimentRequest,
    ExecutableTaskDAG,
    ReActStep,
    ReflectionResult,
    SchemaInfo,
    TaskDAGEdge,
    TaskSpec,
    to_jsonable,
)
from agent import tools as tool_registry


class PlannerFallbackError(RuntimeError):
    """Raised when planning would need to fall back to rule-based TaskSpecs."""

    def __init__(
        self,
        reason: str,
        trace: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.trace = trace or []
        self.context = context or {}


# ---------------------------------------------------------------------------
# System prompt — the core knowledge injected into the global planner
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are GlobalAnomalyPlanner, a single-agent planner for MySQL anomaly propagation experiments.

The user provides the full target_path and the injected_nodes. You must not choose a different path or add injected nodes.
Your job is only to generate executable TaskSpecs for the user-specified injected_nodes.

## Roles

You operate through native chat tool calls. Do not pretend to call tools in text.
Use actual tool_calls for probing, EXPLAIN, TaskSpec building, DAG building, safety checking, and memory reading.
Never call execution or memory-writing tools.

## Anomaly Graph

The graph has three node layers:

### Injectable nodes (can be actively triggered)
- traffic_surge        → extra BenchBase burst workload matching the current background benchmark
- missing_index        → SQL on unindexed columns causing index miss
- improper_sql         → poorly shaped SQL (SELECT *, weak predicates, functions on columns)
- long_tx             → transaction holding locks for long duration
- hot_update           → many concurrent updates to the same hot row
- backup               → mysqldump or ANALYZE creating IO/metadata pressure
- excessive_index      → too many indexes causing write amplification
- resource_cpu         → chaosblade CPU stress
- resource_io          → chaosblade disk IO stress
- resource_memory       → chaosblade memory stress
- resource_network      → chaosblade network stress

### Intermediate observable nodes
- threads_concurrency_up   → Threads_running / Threads_connected increased
- lock_contention          → Innodb_row_lock_waits / lock_wait_time increased
- poor_plan                → EXPLAIN shows type=ALL / filesort / temporary table
- stale_statistics          → rows_estimated vs rows_actual diverged
- sort_hash_spill           → Sort_merge_passes / created_tmp_disk_tables increased
- resource_bottleneck_cpu  → CPU utilization saturated
- resource_bottleneck_io   → disk IO saturation / high io_wait
- maintenance_conflict      → metadata lock evidence from backup/ANALYZE
- table_bloat               → dead tuples / fragmentation accumulated

### Terminal symptom nodes
- slow_query   → slow_query_count_delta >= 1 or p95 latency ratio >= 1.5
- timeout      → Aborted_connects / connection errors increased
- deadlock     → Innodb_deadlocks > 0
- qps_drop     → QPS ratio <= 0.7

## Tool Usage Rules

Use the provided runtime snapshot and baseline context first.
Only call probe_full_snapshot or other probe tools when the provided context is insufficient.
You SHOULD call read_memory before choosing task parameters.
You SHOULD call explain_sql for SQL workload candidates before finalizing them.
You MUST call one TaskSpec builder tool for each user-specified injected node.
You MUST NOT build TaskSpecs for nodes outside injected_nodes.
You MUST call build_task_dag and check_safety before returning the final answer.

### Environment probe tools
- probe_full_snapshot(config, database) → full environment snapshot
- probe_schema(config, database) → table schema only
- probe_table_stats(config, database) → table sizes only
- probe_db_metrics(config, database) → current DB status
- probe_os_metrics() → OS metrics (CPU, memory, disk)
- explain_sql(config, database, sql) → EXPLAIN a candidate SQL

### TaskSpec builder tools (replace SpecialistAgents)
- build_slow_sql_task(config, database, task_id, root_cause, table, column, predicate, sort_column, pattern, limit, concurrency, duration_sec)
- get_benchbase_workload_defaults(benchmark, config_path, database, terminals, rate, duration_sec) -> current BenchBase defaults and constraints
- build_traffic_task(config, profile, task_id)
- build_lock_task(config, database, task_id, table, key_column, holder_concurrency, waiter_concurrency, hold_sec, lock_type)
- build_chaos_task(config, task_id, resource_type, duration_sec, intensity)
- build_backup_task(config, database, task_id, table, tool)

### Orchestration tools
- build_task_dag(task_specs, dependencies) → ExecutableTaskDAG
- check_safety(task_dag, config, current_db_metrics, current_os_metrics) → SafetyResult

### Memory tools
- read_memory(config, anomaly, limit=20) → list of prior memory items

## Planning Process

Step 1 — Inspect: Use the provided runtime snapshot. Call probe tools only if extra evidence is needed.
Step 2 — Memory check: Call read_memory. Incorporate reflection context if provided.
Step 3 — TaskSpec generation: Generate TaskSpecs only for injected_nodes. Prefer large/hot tables and safe parameters.
Step 4 — DAG construction: Call build_task_dag with dependencies only between generated TaskSpecs.
Step 5 — Safety check: Call check_safety.

## Traffic Surge Profile Policy

If injected_nodes contains traffic_surge:
- You MUST use the background workload benchmark from request.workload.benchmark.
- You MUST call get_benchbase_workload_defaults using the background workload config before build_traffic_task.
- You MUST generate a TrafficSurgeProfile and pass it to build_traffic_task(profile=...).
- TrafficSurgeProfile may contain ONLY:
  benchmark, database, config_path, terminals, rate, duration_sec, transaction_mix, mix_template, rationale.
- It MUST NOT contain SQL, queries, lock-holder behavior, slow SQL, custom actions, extra tasks, or ramp stages.
- TrafficSurgeProfile benchmark/database/config_path must match the background workload configuration.
- transaction_mix keys must be legal transaction names for that benchmark.
- On the initial round with no reflection, use the default_transaction_mix, default_rate, default_terminals, and default_duration_sec returned by the defaults tool, except where the user request explicitly overrides duration/terminals.
- After reflection, the LLM may adjust terminals, rate, duration_sec, and transaction_mix based on evaluation feedback.
- rationale must explain why the transaction mix, terminals, rate, and duration support the requested propagation path.

## Slow SQL Diversity Policy

When generating slow SQL TaskSpecs, ensure diversity across:
- Table choice: pick different large tables across retries
- Predicate type: range scan vs equality vs LIKE vs function-on-column
- Join shape: single-table vs multi-table JOIN
- Sort/group: ORDER BY on unindexed column, GROUP BY with no index
- Scan intensity: rows_examined / rows_returned ratio

For missing_index specifically:
- Choose a column that appears in WHERE/ORDER BY but has NO index
- Use range predicate (BETWEEN, >=, <=) or ORDER BY on that column
- Use EXPLAIN to confirm access type degrades to ALL or filesort appears

## Output Format

Return a JSON object with this exact structure:
{
  "target_path": ["node1", "node2", "..."],
  "injected_nodes": ["nodeA", "nodeB"],
  "task_specs": [/* array of TaskSpec dicts returned by build_*_task tools */],
  "dependencies": [["task_id_A", "task_id_B"], ...],
  "dag": {/* result from build_task_dag */},
  "safety_check": {/* result from check_safety tool */},
  "reasoning": "brief explanation of how each TaskSpec supports its injected node"
}

Only return this JSON — no extra text outside it.
"""


# ---------------------------------------------------------------------------
# Global Planner
# ---------------------------------------------------------------------------

class GlobalPlanner:
    """
    Single ReAct-based planner.  No SpecialistAgents — all task generation
    logic is handled via tools inside the ReAct loop.
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._react_trace: list[ReActStep] = []
        self.last_plan_payload: dict[str, Any] = {}

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
            db_metrics=snapshot_data.get("db_metrics", {}),
            workload_status=snapshot_data.get("workload", {}),
            os_metrics=snapshot_data.get("os_metrics", {}),
            db_version=snapshot_data.get("db_version", ""),
            max_connections=int(snapshot_data.get("db_metrics", {}).get("max_connections", 100)),
            react_trace=[],
        )

    def plan(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        memory_items: Optional[List[dict]] = None,
        reflection: Optional[ReflectionResult] = None,
    ) -> tuple[ExecutableTaskDAG, EnvironmentSnapshot, list[ReActStep]]:
        """
        Run the ReAct planning loop to produce an ExecutableTaskDAG.

        Returns (dag, snapshot, react_trace).
        """
        self._react_trace = []
        memory_items = memory_items or []
        self.last_plan_payload = {}
        self._validate_request(request)

        # ---- ReAct Step 1: Inspect (already done, just record it) ----
        self._trace(
            round=1,
            thought="Environment snapshot collected. "
                    f"Schema has {len(snapshot.schema.tables) if snapshot.schema else 0} tables. "
                    f"DB version: {snapshot.db_version}. "
                    f"Max connections: {snapshot.max_connections}.",
            action="probe_full_snapshot",
            observe=f"Tables: {list(snapshot.schema.tables.keys())[:5] if snapshot.schema else []}",
        )

        task_specs, dependencies, dag_dict, plan_payload = self._run_react_planner(
            request=request,
            snapshot=snapshot,
            memory_items=memory_items,
            reflection=reflection,
        )
        if not dag_dict:
            dag_dict = tool_registry.build_task_dag(task_specs, dependencies)
        dag = self._dict_to_dag(dag_dict)

        self._trace(
            round=len(self._react_trace) + 1,
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
        }
        snapshot.react_trace = self._react_trace
        return dag, snapshot, self._react_trace

    def reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        memory_items: list[dict],
    ) -> ReflectionResult:
        """Generate a ReflectionResult from a failed evaluation."""
        from agent.reflection import SelfReflection

        reflector = SelfReflection(self.config)
        return reflector.reflect(evaluation, request, memory_items)

    def _run_react_planner(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        memory_items: list[dict],
        reflection: ReflectionResult | None,
    ) -> tuple[list[dict[str, Any]], list[list[str]], dict[str, Any], dict[str, Any]]:
        """Use native tool-calling to generate TaskSpecs for injected nodes."""
        if not self.config.planner_enabled or not self.config.openai_api_key:
            missing = []
            if not self.config.planner_enabled:
                missing.append("planner_enabled=false")
            if not self.config.openai_api_key:
                missing.append("missing OPENAI_API_KEY")
            reason = "Planner fallback blocked: " + ", ".join(missing)
            self._trace(len(self._react_trace) + 1, reason, "fallback_blocked")
            raise PlannerFallbackError(
                reason=reason,
                trace=[s.to_dict() for s in self._react_trace],
                context={
                    "target_path": request.target_path,
                    "injected_nodes": request.injected_nodes,
                    "target_database": request.target_database,
                },
            )

        context = self._build_llm_context(request, snapshot, memory_items, reflection)
        try:
            response = tool_registry.chat_tool_calling_loop(
                config=self.config,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=context,
                max_steps=12,
                temperature=self.config.planner_temperature,
            )
        except tool_registry.LLMTimeoutError as exc:
            reason = f"Planner LLM timeout: {exc}"
            self._trace(len(self._react_trace) + 1, reason, "llm_timeout")
            raise PlannerFallbackError(
                reason=reason,
                trace=[s.to_dict() for s in self._react_trace],
                context={
                    "target_path": request.target_path,
                    "injected_nodes": request.injected_nodes,
                    "target_database": request.target_database,
                },
            ) from exc
        if response.get("error"):
            self._trace_from_tool_loop(response.get("trace", []))
            reason = f"Planner fallback blocked after tool loop failure: {response['error']}"
            self._trace(len(self._react_trace) + 1, reason, "fallback_blocked")
            raise PlannerFallbackError(
                reason=reason,
                trace=response.get("trace", []),
                context={
                    "target_path": request.target_path,
                    "injected_nodes": request.injected_nodes,
                    "target_database": request.target_database,
                },
            )

        self._trace_from_tool_loop(response.get("trace", []))
        payload = response.get("json_payload") or {}
        task_specs = self._filter_task_specs(payload.get("task_specs", []), request)
        dependencies = self._filter_dependencies(
            payload.get("dependencies") or self._build_dependencies(request.target_path, task_specs),
            task_specs,
        )
        # Rebuild locally after filtering, so a final JSON cannot smuggle extra tasks.
        dag_dict = tool_registry.build_task_dag(task_specs, dependencies)
        self._trace(
            round=len(self._react_trace) + 1,
            thought=f"ReAct generated {len(task_specs)} TaskSpecs for injected nodes {request.injected_nodes}",
            action="final_plan_validation",
            observe=json.dumps({"task_ids": [t.get("task_id") for t in task_specs]}, ensure_ascii=False),
        )
        return task_specs, dependencies, dag_dict, payload

    def _generate_task_specs_fallback(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
    ) -> list[dict[str, Any]]:
        """Rule-based TaskSpec generation when LLM is unavailable."""
        task_specs: list[dict[str, Any]] = []
        tables = snapshot.schema.tables if snapshot.schema else {}

        # Pick the largest table for slow SQL tasks
        largest_table = ""
        if tables:
            largest_table = list(tables.keys())[0]

        for node_id in request.injected_nodes:
            n = ANOMALY_GRAPH.node(node_id)
            if n and n.injectable:
                if node_id in ("missing_index", "improper_sql", "excessive_index"):
                    ts = tool_registry.build_slow_sql_task(
                        config=self.config,
                        database=request.target_database,
                        task_id=f"slow_sql_{node_id}",
                        root_cause=node_id,
                        table=largest_table,
                        pattern="range_scan" if node_id == "missing_index" else "large_scan",
                        duration_sec=30,
                    )
                    task_specs.append(ts)
                elif node_id == "traffic_surge":
                    workload = request.workload or {}
                    duration = min(float(request.max_duration_sec), float(workload.get("injection_observe_sec", 60) or 60))
                    profile = {
                        "benchmark": str(workload.get("benchmark") or "tpcc").lower(),
                        "database": workload.get("database") or request.target_database,
                        "config_path": workload.get(
                            "config_path",
                            ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
                        ),
                        "terminals": max(1, int(workload.get("terminals") or 16)),
                        "rate": 100.0,
                        "duration_sec": duration,
                        "transaction_mix": dict(tool_registry.BENCHBASE_BENCHMARKS[str(workload.get("benchmark") or "tpcc").lower()]["default_mix"]),
                        "mix_template": "workload_default",
                        "rationale": "Unused rule fallback profile retained only for private compatibility.",
                    }
                    ts = tool_registry.build_traffic_task(
                        config=self.config,
                        task_id=f"traffic_{node_id}",
                        profile=profile,
                    )
                    task_specs.append(ts)
                elif node_id in ("long_tx", "hot_update"):
                    ts = tool_registry.build_lock_task(
                        config=self.config,
                        database=request.target_database,
                        task_id=f"lock_{node_id}",
                        root_cause=node_id,
                        table=largest_table,
                        key_column="id",
                        holder_concurrency=2,
                        waiter_concurrency=8,
                        hold_sec=30.0,
                    )
                    task_specs.append(ts)
                elif node_id.startswith("resource_"):
                    resource = node_id.replace("resource_", "")
                    ts = tool_registry.build_chaos_task(
                        config=self.config,
                        task_id=f"chaos_{node_id}",
                        root_cause=node_id,
                        resource_type=resource,
                        duration_sec=30,
                    )
                    task_specs.append(ts)
                elif node_id == "backup":
                    ts = tool_registry.build_backup_task(
                        config=self.config,
                        database=request.target_database,
                        task_id=f"backup_{node_id}",
                        root_cause=node_id,
                        table=largest_table,
                    )
                    task_specs.append(ts)
        return task_specs

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
    # ReAct tracing
    # -------------------------------------------------------------------------

    def _trace(self, round: int, thought: str, action: str, observe: str = "") -> None:
        step = ReActStep(
            round=round,
            thought=thought,
            action=action,
            observe=observe,
            decision="",
        )
        self._react_trace.append(step)

    def _trace_from_tool_loop(self, trace: list[dict[str, Any]]) -> None:
        for item in trace:
            action = str(item.get("tool", "tool_call"))
            thought = "LLM requested planning tool" if action != "final_answer" else "LLM returned final plan JSON"
            observe = json.dumps({k: v for k, v in item.items() if k not in {"result"}}, ensure_ascii=False, default=str)
            if "result" in item:
                observe = json.dumps(item.get("result"), ensure_ascii=False, default=str)[:4000]
            self._trace(round=len(self._react_trace) + 1, thought=thought, action=action, observe=observe)

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
                    round=len(self._react_trace) + 1,
                    thought=f"Dropped TaskSpec for non-requested injected node: {root or '<missing>'}",
                    action="filter_task_spec",
                    observe=json.dumps({"task_id": spec.get("task_id"), "root_cause": root}, ensure_ascii=False),
                )
                continue
            if root in seen:
                raise ValueError(f"multiple TaskSpecs generated for injected node '{root}'")
            if root == "traffic_surge":
                self._validate_traffic_task_spec(spec, request)
            seen.add(root)
            filtered.append(spec)
        missing = allowed - seen
        if missing:
            raise ValueError(f"missing TaskSpecs for injected nodes: {', '.join(sorted(missing))}")
        return filtered

    @staticmethod
    def _validate_traffic_task_spec(spec: dict[str, Any], request: ExperimentRequest) -> None:
        actions = spec.get("actions") or []
        if len(actions) != 1 or not isinstance(actions[0], dict):
            raise ValueError("traffic_surge TaskSpec must contain exactly one benchbase_burst action")
        action = actions[0]
        if action.get("kind") != "benchbase_burst":
            raise ValueError("traffic_surge TaskSpec must use benchbase_burst, not workload_ramp/custom actions")
        metadata = spec.get("metadata") or {}
        action_profile = action.get("profile")
        metadata_profile = metadata.get("traffic_surge_profile")
        if action_profile is None or metadata_profile is None:
            raise ValueError("traffic_surge TaskSpec must include TrafficSurgeProfile in action and metadata")
        normalized_action = tool_registry.validate_traffic_surge_profile(action_profile)
        normalized_metadata = tool_registry.validate_traffic_surge_profile(metadata_profile)
        if normalized_action != normalized_metadata:
            raise ValueError("traffic_surge action profile and metadata profile must match")
        workload = request.workload or {}
        if workload.get("enabled"):
            expected_benchmark = str(workload.get("benchmark") or "").lower()
            if expected_benchmark and normalized_action["benchmark"] != expected_benchmark:
                raise ValueError(
                    "traffic_surge benchmark must match background workload "
                    f"({normalized_action['benchmark']} != {expected_benchmark})"
                )
            expected_database = str(workload.get("database") or request.target_database)
            if expected_database and normalized_action["database"] != expected_database:
                raise ValueError(
                    "traffic_surge database must match background workload "
                    f"({normalized_action['database']} != {expected_database})"
                )
            expected_config_path = str(workload.get("config_path") or "")
            if expected_config_path and normalized_action["config_path"] != expected_config_path:
                raise ValueError("traffic_surge config_path must match background workload config_path")

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
{json.dumps(request.workload, ensure_ascii=False, default=str)}

## User-Specified Propagation Path
{' -> '.join(request.target_path)}

## Relevant Graph Edges
{graph_edges}

## Memory (recent prior attempts)
{memory_summary if memory_summary else 'No prior memory.'}

## Reflection Context For Retry
{reflection_summary}

## Your Task
Use tool calls to generate task_specs only for injected_nodes.
Do not add, remove, or reorder target_path or injected_nodes.
Return the JSON format described in your system prompt.
"""
        return context
