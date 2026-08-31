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


ALLOWED_PLANNER_ACTION_KINDS = {
    "raw_sql_workload",
    "raw_transaction_script",
    "raw_command",
    "logical_backup_command",
    "benchbase_burst_command",
    # Legacy action kinds remain accepted for old artifacts and transitional plans.
    "sql_workload",
    "benchbase_burst",
    "lock_conflict",
    "chaosblade",
    "logical_backup",
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
- traffic_surge        → direct TaskSpec using benchbase_burst_command or raw_command
- missing_index        → direct SQL workload on unindexed columns
- improper_sql         → direct poorly shaped SQL workload
- long_tx              → direct transaction script holding locks
- hot_update           → direct transaction script with concurrent updates
- backup               → direct backup command or SQL maintenance workload
- resource_cpu         → direct ChaosBlade command
- resource_io          → direct ChaosBlade command
- resource_memory      → direct ChaosBlade command
- resource_network     → direct ChaosBlade command
- metadata_lock        → direct transaction/DDL script creating metadata lock waits
- table_lock           → direct table lock or table-level blocking script
- network_latency      → direct ChaosBlade network drop command scoped to MySQL traffic
- disk_full_or_pressure → controlled disk pressure command; never fill the system disk
- deadlock_storm       → direct conflicting transaction scripts that create deadlocks
- large_temp_table     → direct SQL workload creating temp tables / filesort / GROUP BY spill
- redo_log_pressure    → direct high-concurrency write workload creating redo/fsync pressure
- connection_storm     → direct bounded burst of concurrent/short-lived MySQL connections

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
- metadata_lock_wait        → processlist/performance_schema metadata lock wait
- connection_pressure       → Threads_connected / Max_used_connections / connect errors increased
- temp_table_spill          → Created_tmp_disk_tables / Sort_merge_passes increased
- buffer_pool_pressure      → buffer pool reads/misses or memory pressure increased
- redo_log_flush_stall      → commit/write latency or Innodb_log_waits increased
- binlog_flush_stall        → binlog cache/flush pressure or commit latency increased
- deadlock_detected         → Innodb_deadlocks increased
- disk_saturation           → IO wait / disk util / disk-backed temp activity increased
- network_stall             → connection errors, timeout, or throughput loss from network drop

### Terminal symptom nodes
- slow_query   → at least one new target-database entry appears in the injection window's MySQL slow log
- timeout      → Aborted_connects / connection errors increased
- deadlock     → Innodb_deadlocks > 0
- qps_drop     → injection average QPS < baseline average QPS * 0.7
- commit_latency_up      → write or commit latency increased
- connection_error       → connection failures / Aborted_connects increased
- write_throughput_drop  → injection average TPS < baseline average TPS * 0.7

## Tool Usage Rules

Use the provided runtime snapshot and baseline context first.
Only call probe_full_snapshot or other probe tools when the provided context is insufficient.
You SHOULD call read_memory before choosing task parameters.
You SHOULD call explain_sql for SQL workload candidates before finalizing them.
You MUST directly write one complete TaskSpec JSON object for each user-specified injected node in the final answer.
You MUST NOT write TaskSpecs for nodes outside injected_nodes.
You MUST call build_task_dag and check_safety before returning the final answer.

### Environment probe tools
- probe_full_snapshot(config, database) → full environment snapshot
- probe_schema(config, database) → table schema only
- probe_table_stats(config, database) → table sizes only
- probe_db_metrics(config, database) → current DB status
- probe_os_metrics() → OS metrics (CPU, memory, disk)
- probe_workload(config, database, interval_sec) → current workload QPS/TPS sample
- explain_sql(config, database, sql) → EXPLAIN a candidate SQL

### Orchestration tools
- build_task_dag(task_specs, dependencies) → ExecutableTaskDAG
- check_safety(task_dag, config, current_db_metrics, current_os_metrics) → SafetyResult

### Memory tools
- read_memory(config, anomaly, limit=20) → list of prior memory items

## Planning Process

Step 1 — Inspect: Use the provided runtime snapshot. Call probe tools only if extra evidence is needed.
Step 2 — Memory check: Call read_memory. Incorporate reflection context if provided.
Step 3 — TaskSpec generation: Directly write TaskSpecs only for injected_nodes. Prefer large/hot tables and bounded parameters.
Step 4 — DAG construction: Call build_task_dag with dependencies only between generated TaskSpecs.
Step 5 — Safety check: Call check_safety.

## Direct Raw Action TaskSpec Policy

Do not call specialist builder tools. They are intentionally unavailable.
In the final JSON, each TaskSpec must include:
- task_id
- task_type
- actions
- expected_metrics
- success_criteria
- risk_assessment
- metadata.root_cause, exactly equal to its injected node

Allowed new raw action kinds:
- raw_sql_workload: {kind, database, sql, concurrency, duration_sec}
- raw_transaction_script: {kind, database, scripts, duration_sec, concurrency}
- raw_command: {kind, command, duration_sec, cwd, env, cleanup_command}
- logical_backup_command: {kind, database, command, duration_sec, output_path}
- benchbase_burst_command: {kind, benchmark, database, command, duration_sec, terminals, rate}

Commands must be argv arrays, never shell strings.
SQL tasks should call explain_sql before finalizing candidate SQL when possible.
Traffic surge should use benchbase_burst_command and match the background workload benchmark/database.
For benchbase_burst_command, generate an argv command using the background workload java_bin and jar_path, e.g. [java_bin, "-jar", jar_path, "-b", benchmark, "-c", config_path, "--execute=true"].
Do not use the BenchBase directory as command[0]; command[0] must be java/java_bin.
Lock contention and deadtuple should use raw_transaction_script.
metadata_lock should use raw_transaction_script/raw_sql_workload to hold a table transaction and run bounded DDL/LOCK TABLE against the same table.
table_lock should use raw_transaction_script with LOCK TABLES or equivalent table-level blocking and explicit UNLOCK cleanup.
deadlock_storm should use raw_transaction_script with two roles updating the same rows in opposite order.
For deadlock_storm, include expected_error_codes: [1213] or expected_error: "deadlock" on the raw_transaction_script action because MySQL deadlock error 1213 is expected evidence.
large_temp_table should use raw_sql_workload with GROUP BY, ORDER BY, DISTINCT, or JOIN that EXPLAIN shows Using temporary or Using filesort.
redo_log_pressure should use raw_sql_workload/raw_transaction_script with bounded high-concurrency INSERT/UPDATE/COMMIT on target tables.
connection_storm should use bounded raw_sql_workload or a safe command/script that opens many MySQL connections inside duration_sec.
Resource limitations must use raw_command with a ChaosBlade argv command. The command[0] must be the configured ChaosBlade binary path from RuntimeConfig.chaosblade_path, not bare "blade".
For resource_cpu/resource_io/resource_memory/resource_network/network_latency/disk_full_or_pressure, generate a unique ChaosBlade --uid and a matching cleanup_command such as [configured_chaosblade_path, "destroy", uid].
Use the current Darwin ChaosBlade command forms:
- resource_cpu: [configured_chaosblade_path, "create", "cpu", "fullload", "--cpu-percent", "90", "--timeout", duration_sec, "--uid", uid]
- resource_memory: [configured_chaosblade_path, "create", "mem", "load", "--mode", "ram", "--mem-percent", "80", "--timeout", duration_sec, "--uid", uid]
- resource_io: [configured_chaosblade_path, "create", "disk", "burn", "--read", "--write", "--path", "/tmp", "--size", "100M", "--timeout", duration_sec, "--uid", uid]
- network_latency: [configured_chaosblade_path, "create", "network", "drop", "--destination-port", "3306", "--network-traffic", "out", "--timeout", duration_sec, "--uid", uid]
- disk_full_or_pressure: prefer disk burn under a safe temp path; do not generate an unbounded disk fill command
Do not attempt to fill the system disk for disk_full_or_pressure.
Do not use obsolete resource command forms or flags: "cpu load", "disk fill", "--duration", "--process-name", "--read-bps", or "--write-bps".
Backup should use logical_backup_command.

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
                max_steps=15,
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
        raw_task_specs = payload.get("task_specs") or []
        try:
            normalized_payload = tool_registry.canonicalize_task_dag_runtime_paths(
                self.config,
                {"tasks": {
                    str(spec.get("task_id") or f"task_{index}"): spec
                    for index, spec in enumerate(raw_task_specs)
                    if isinstance(spec, dict)
                }},
                expected_workload=self._planner_workload_context(request),
            )
            normalized_task_specs = list((normalized_payload.get("tasks") or {}).values())
            self._normalize_task_spec_root_aliases(normalized_task_specs, request)
            planning_round = "retry" if reflection is not None else "initial"
            planner_duration_limit = self._planner_duration_limit(request)
            if planner_duration_limit is not None:
                duration_clamps = self._clamp_planner_task_durations(
                    normalized_task_specs,
                    planner_duration_limit,
                    planning_round,
                )
                if duration_clamps:
                    self._trace(
                        round=len(self._react_trace) + 1,
                        thought=(
                            "Planner TaskSpec durations exceeded the allowed window and "
                            "were clamped before final validation."
                        ),
                        action="clamp_planner_duration",
                        observe=json.dumps(duration_clamps, ensure_ascii=False),
                    )
            task_specs = self._filter_task_specs(
                normalized_task_specs,
                request,
            )
            dependencies = self._filter_dependencies(
                payload.get("dependencies") or self._build_dependencies(request.target_path, task_specs),
                task_specs,
            )
            # Rebuild locally after filtering, so a final JSON cannot smuggle extra tasks.
            dag_dict = tool_registry.build_task_dag(task_specs, dependencies)
        except (TypeError, ValueError, AttributeError) as exc:
            reason = f"Planner output schema invalid: {exc}"
            self._trace(len(self._react_trace) + 1, reason, "fallback_blocked")
            raise PlannerFallbackError(
                reason=reason,
                trace=[s.to_dict() for s in self._react_trace],
                context={
                    "target_path": request.target_path,
                    "injected_nodes": request.injected_nodes,
                    "target_database": request.target_database,
                },
            ) from exc
        self._trace(
            round=len(self._react_trace) + 1,
            thought=f"ReAct generated {len(task_specs)} TaskSpecs for injected nodes {request.injected_nodes}",
            action="final_plan_validation",
            observe=json.dumps({"task_ids": [t.get("task_id") for t in task_specs]}, ensure_ascii=False),
        )
        return task_specs, dependencies, dag_dict, payload

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
            self._validate_direct_task_spec(spec, request)
            seen.add(root)
            filtered.append(spec)
        missing = allowed - seen
        if missing:
            raise ValueError(f"missing TaskSpecs for injected nodes: {', '.join(sorted(missing))}")
        return filtered

    def _validate_direct_task_spec(self, spec: dict[str, Any], request: ExperimentRequest) -> None:
        required = ("task_id", "task_type", "actions", "expected_metrics", "success_criteria", "risk_assessment", "metadata")
        missing = [name for name in required if name not in spec]
        if missing:
            raise ValueError(f"TaskSpec missing required fields: {', '.join(missing)}")
        metadata = spec.get("metadata") or {}
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
        from agent.config import resolve_runtime_path

        workload = dict(request.workload or {})
        workload["jar_path"] = self.config.benchbase_jar_path
        workload["java_bin"] = self.config.benchbase_java_bin
        config_path = str(workload.get("config_path") or "")
        if config_path:
            workload["config_path"] = resolve_runtime_path(config_path)
        return workload
