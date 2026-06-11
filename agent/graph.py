"""
Hardcoded anomaly propagation graph for MySQL.

This module defines the complete AnomalyGraph used by the global planner.
Nodes are divided into three layers:
  - Injectable  : root causes that can be actively triggered
  - Intermediate: observable intermediate states / mechanisms
  - Symptom     : terminal anomaly symptoms

Each node carries evidence_rules that define how to evaluate whether it was hit.
"""

from __future__ import annotations

from agent.types import (
    AnomalyEdge,
    AnomalyGraph,
    AnomalyNode,
    EdgeRelation,
    EvidenceRule,
    NodeCategory,
)


# ---------------------------------------------------------------------------
# Evidence rule helpers
# ---------------------------------------------------------------------------

def _r(
    metric: str,
    operator: str,
    threshold: float,
    *,
    required: bool = False,
    weight: float = 1.0,
    baseline_adjusted: bool = True,
) -> EvidenceRule:
    return EvidenceRule(
        metric=metric,
        operator=operator,  # type: ignore
        threshold=threshold,
        baseline_adjusted=baseline_adjusted,
        required=required,
        weight=weight,
    )


# ---------------------------------------------------------------------------
# Node definitions
# ---------------------------------------------------------------------------

NODES: dict[str, AnomalyNode] = {

    # === Injectable nodes ================================================

    "traffic_surge": AnomalyNode(
        node_id="traffic_surge",
        label="Traffic Surge",
        description="External traffic spike causing increased concurrent sessions.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_traffic_task",
        planner_notes=(
            "Increase active sessions and Threads_running with an additional BenchBase "
            "burst client. Generate a TrafficSurgeProfile only: terminals, rate, duration_sec, "
            "and the current benchmark transaction mix. Do not use custom SQL or lock-holder behavior."
        ),
        evidence_rules=[
            _r("Threads_connected", "ratio_up", 1.5, required=True, weight=1.5),
            _r("Threads_running", "ratio_up", 1.5, required=False, weight=1.0),
            _r("active_sessions_delta", ">", 5, required=False, weight=0.8),
        ],
    ),

    "missing_index": AnomalyNode(
        node_id="missing_index",
        label="Missing Index",
        description="Slow query caused by filtering or sorting on a column without an index.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_slow_sql_task",
        planner_notes=(
            "Pick a large table. Find unindexed columns of appropriate type (int/date/varchar). "
            "Use range predicate on the unindexed column, or ORDER BY on the unindexed column "
            "with a small LIMIT to force filesort. Diversity: vary table, predicate type, "
            "sort column, and whether a JOIN is involved."
        ),
        evidence_rules=[
            _r("rows_examined_ratio", ">", 2.0, required=True, weight=1.5),
            _r("slow_query_count_delta", ">=", 1, required=False, weight=1.0),
            _r("p95_latency_ratio", "ratio_up", 1.5, required=False, weight=0.8),
            _r("explain_access_type", "contains", "ALL", required=False, weight=1.0),
        ],
    ),

    "improper_sql": AnomalyNode(
        node_id="improper_sql",
        label="Improper SQL Shape",
        description="Poorly written SQL causing excessive scanning or suboptimal plans.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_slow_sql_task",
        planner_notes=(
            "Use SELECT *, low-selectivity predicates, functions on columns, "
            "unnecessary ORDER BY/GROUP BY, or multi-table JOIN with weak conditions. "
            "Diversity: vary from SELECT * vs weak predicates vs function-on-column."
        ),
        evidence_rules=[
            _r("rows_examined_ratio", ">", 3.0, required=True, weight=1.5),
            _r("slow_query_count_delta", ">=", 1, required=False, weight=1.0),
            _r("p95_latency_ratio", "ratio_up", 2.0, required=False, weight=1.0),
        ],
    ),

    "long_tx": AnomalyNode(
        node_id="long_tx",
        label="Long Transaction",
        description="A transaction that holds locks for an extended duration.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_lock_task",
        planner_notes=(
            "BEGIN; UPDATE on a hot row; SLEEP for many seconds; COMMIT. "
            "This blocks concurrent transactions waiting on the same row."
        ),
        evidence_rules=[
            _r("Innodb_row_lock_time", "ratio_up", 2.0, required=False, weight=1.0),
            _r("blocked_session_count", ">", 1, required=True, weight=1.5),
            _r("lock_wait_time_delta", ">", 0, required=False, weight=0.8),
        ],
    ),

    "hot_update": AnomalyNode(
        node_id="hot_update",
        label="Hot Row Update Storm",
        description="Many concurrent transactions updating the same hot row.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_lock_task",
        planner_notes=(
            "Pick a primary-key or high-frequency-access column. "
            "Spawn holder/waiter threads. Holder locks the row with FOR UPDATE; "
            "waiters continuously UPDATE the same row. Diversity: vary table, "
            "predicate column, and waiter concurrency."
        ),
        evidence_rules=[
            _r("Innodb_row_lock_waits", "ratio_up", 2.0, required=True, weight=1.5),
            _r("Innodb_row_lock_time_avg", "ratio_up", 1.5, required=False, weight=1.0),
            _r("lock_wait_time_delta", ">", 0, required=False, weight=0.8),
        ],
    ),

    "backup": AnomalyNode(
        node_id="backup",
        label="Backup / Maintenance",
        description="Logical backup (mysqldump) or ANALYZE creating IO pressure and maintenance conflicts.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_backup_task",
        planner_notes=(
            "Run mysqldump on the largest table, or run ANALYZE TABLE on a hot table. "
            "mysqldump takes a shared table lock which can conflict with active transactions."
        ),
        evidence_rules=[
            _r("io_wait_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("created_tmp_disk_tables", "ratio_up", 1.5, required=False, weight=0.8),
            _r("metadata_lock_evidence", "exists", True, required=False, weight=1.0),
        ],
    ),

    "excessive_index": AnomalyNode(
        node_id="excessive_index",
        label="Excessive Indexes",
        description="Too many indexes causing write amplification during DML.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_slow_sql_task",
        planner_notes=(
            "INSERT/UPDATE on a table with many redundant indexes. "
            "Simulated by repeated UPDATEs that trigger index maintenance overhead."
        ),
        evidence_rules=[
            _r("p95_latency_ratio", "ratio_up", 1.3, required=False, weight=1.0),
            _r("index maintenance_overhead", "exists", True, required=False, weight=0.8),
        ],
    ),

    "resource_cpu": AnomalyNode(
        node_id="resource_cpu",
        label="CPU Bottleneck",
        description="OS-level CPU saturation injected via chaosblade.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes="Use chaosblade to stress CPU for the configured duration.",
        evidence_rules=[
            _r("cpu_usage_ratio", "ratio_up", 1.5, required=True, weight=1.5),
            _r("load_average_1m", ">", 8.0, required=False, weight=1.0),
        ],
    ),

    "resource_io": AnomalyNode(
        node_id="resource_io",
        label="IO Bottleneck",
        description="OS-level disk IO pressure injected via chaosblade.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes="Use chaosblade disk fill or IO stress for the configured duration.",
        evidence_rules=[
            _r("io_wait_ratio", "ratio_up", 2.0, required=True, weight=1.5),
            _r("disk_util", ">", 0.8, required=False, weight=1.0),
        ],
    ),

    "resource_memory": AnomalyNode(
        node_id="resource_memory",
        label="Memory Pressure",
        description="OS-level memory pressure injected via chaosblade.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes="Use chaosblade memory stress for the configured duration.",
        evidence_rules=[
            _r("memory_usage_ratio", "ratio_up", 1.3, required=True, weight=1.5),
            _r("buffer_pool_pressure", "ratio_up", 1.3, required=False, weight=1.0),
        ],
    ),

    "resource_network": AnomalyNode(
        node_id="resource_network",
        label="Network Bottleneck",
        description="OS-level network latency/injection via chaosblade.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes="Use chaosblade network latency/drop for the configured duration.",
        evidence_rules=[
            _r("network_retransmit_ratio", "ratio_up", 2.0, required=False, weight=1.0),
            _r("connection_error_ratio", "ratio_up", 1.5, required=False, weight=1.0),
        ],
    ),

    # === Intermediate nodes ===========================================

    "threads_concurrency_up": AnomalyNode(
        node_id="threads_concurrency_up",
        label="Thread Concurrency Up",
        description="Increased Threads_running indicating high query concurrency.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Threads_running", "ratio_up", 1.5, required=True, weight=1.5),
            _r("active_sessions_delta", ">", 3, required=False, weight=1.0),
        ],
    ),

    "lock_contention": AnomalyNode(
        node_id="lock_contention",
        label="Lock Contention",
        description="Increased row-level or table-level lock waits.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Innodb_row_lock_waits", "ratio_up", 2.0, required=True, weight=1.5),
            _r("Innodb_row_lock_time_avg", "ratio_up", 1.5, required=False, weight=1.0),
            _r("lock_wait_time_delta", ">", 0, required=False, weight=0.8),
        ],
    ),

    "poor_plan": AnomalyNode(
        node_id="poor_plan",
        label="Suboptimal Execution Plan",
        description="Query optimizer chooses a bad plan: full scan, filesort, temp table.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("explain_access_type", "contains", "ALL", required=True, weight=1.5),
            _r("explain_extra", "contains", "filesort", required=False, weight=1.0),
            _r("explain_extra", "contains", "temporary", required=False, weight=1.0),
        ],
    ),

    "stale_statistics": AnomalyNode(
        node_id="stale_statistics",
        label="Stale Statistics",
        description="Table statistics are outdated, causing inaccurate row estimates.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("rows_estimated_vs_actual_ratio", ">", 10.0, required=True, weight=1.5),
            _r("explain_rows", "ratio_up", 5.0, required=False, weight=1.0),
        ],
    ),

    "sort_hash_spill": AnomalyNode(
        node_id="sort_hash_spill",
        label="Sort/Hash Spill to Disk",
        description="In-memory sort or hash operation spills to disk due to memory pressure.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Sort_merge_passes", "ratio_up", 2.0, required=True, weight=1.5),
            _r("created_tmp_disk_tables", "ratio_up", 2.0, required=False, weight=1.0),
        ],
    ),

    "resource_bottleneck_cpu": AnomalyNode(
        node_id="resource_bottleneck_cpu",
        label="CPU Resource Bottleneck",
        description="CPU utilization saturated, slowing all queries.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("cpu_usage_ratio", "ratio_up", 1.5, required=True, weight=1.5),
            _r("load_average_1m", ">", 8.0, required=False, weight=1.0),
        ],
    ),

    "resource_bottleneck_io": AnomalyNode(
        node_id="resource_bottleneck_io",
        label="IO Resource Bottleneck",
        description="Disk IO saturation, causing high io_wait.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("io_wait_ratio", "ratio_up", 2.0, required=True, weight=1.5),
            _r("disk_util", ">", 0.8, required=False, weight=1.0),
        ],
    ),

    "maintenance_conflict": AnomalyNode(
        node_id="maintenance_conflict",
        label="Maintenance Conflict",
        description="Backup or ANALYZE conflicts with active transactions via MDL.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("metadata_lock_evidence", "exists", True, required=True, weight=1.5),
            _r("thread_state", "contains", "Waiting for table metadata lock", required=False, weight=1.0),
        ],
    ),

    "table_bloat": AnomalyNode(
        node_id="table_bloat",
        label="Table Bloat",
        description="Dead tuples accumulate; VACUUM lag causes performance degradation.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("table_fragmentation_ratio", ">", 1.3, required=False, weight=1.0),
            _r("dead_tuple_count", "ratio_up", 2.0, required=False, weight=0.8),
        ],
    ),

    # === Symptom nodes =================================================

    "slow_query": AnomalyNode(
        node_id="slow_query",
        label="Slow Query",
        description="Query latency significantly increased; slow query log entries appear.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("slow_query_count_delta", ">=", 1, required=True, weight=2.0),
            _r("p95_latency_ratio", "ratio_up", 1.5, required=False, weight=1.5),
            _r("avg_latency_ratio", "ratio_up", 1.5, required=False, weight=1.0),
        ],
    ),

    "timeout": AnomalyNode(
        node_id="timeout",
        label="Query Timeout",
        description="Queries start timing out; connection errors increase.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("Aborted_connects", "ratio_up", 2.0, required=False, weight=1.0),
            _r("connection_error_ratio", "ratio_up", 2.0, required=False, weight=1.0),
            _r("timeout_error_count", ">", 0, required=True, weight=1.5),
        ],
    ),

    "deadlock": AnomalyNode(
        node_id="deadlock",
        label="Deadlock",
        description="InnoDB deadlock detected; one transaction rolled back.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("Innodb_deadlocks", ">", 0, required=True, weight=2.0),
            _r("innodb_lock_wait_timeouts", "ratio_up", 1.5, required=False, weight=0.8),
        ],
    ),

    "qps_drop": AnomalyNode(
        node_id="qps_drop",
        label="QPS Drop",
        description="Overall query throughput decreases significantly.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("qps_ratio", "ratio_down", 0.7, required=True, weight=1.5),
            _r("tps_ratio", "ratio_down", 0.7, required=False, weight=1.0),
        ],
    ),

    "lock_wait": AnomalyNode(
        node_id="lock_wait",
        label="Lock Wait",
        description="Transactions waiting for row locks; wait time increases.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("lock_wait_time_delta", ">", 0, required=True, weight=1.5),
            _r("blocked_session_count", ">", 0, required=False, weight=1.0),
            _r("Innodb_row_lock_waits", "ratio_up", 2.0, required=False, weight=0.8),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Edge definitions
# ---------------------------------------------------------------------------

EDGES: list[AnomalyEdge] = [
    # traffic_surge propagation
    AnomalyEdge("traffic_surge", "threads_concurrency_up", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("traffic_surge", "resource_bottleneck_cpu", EdgeRelation.AMPLIFIES, 0.7, False),
    AnomalyEdge("threads_concurrency_up", "lock_contention", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("lock_contention", "slow_query", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("lock_contention", "timeout", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("slow_query", "qps_drop", EdgeRelation.CAUSES, 0.9, True),

    # missing_index propagation
    AnomalyEdge("missing_index", "poor_plan", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("poor_plan", "slow_query", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("poor_plan", "sort_hash_spill", EdgeRelation.AMPLIFIES, 0.7, False),
    AnomalyEdge("sort_hash_spill", "slow_query", EdgeRelation.CAUSES, 0.8, False),

    # improper_sql propagation
    AnomalyEdge("improper_sql", "sort_hash_spill", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("improper_sql", "rows_examined_ratio", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("sort_hash_spill", "slow_query", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("improper_sql", "slow_query", EdgeRelation.CAUSES, 0.8, False),

    # long_tx propagation
    AnomalyEdge("long_tx", "lock_contention", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("long_tx", "threads_concurrency_up", EdgeRelation.CAUSES, 0.6, False),

    # hot_update propagation
    AnomalyEdge("hot_update", "lock_contention", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("hot_update", "lock_wait", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("hot_update", "deadlock", EdgeRelation.CAUSES, 0.5, False),

    # backup propagation
    AnomalyEdge("backup", "maintenance_conflict", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("backup", "lock_contention", EdgeRelation.AMPLIFIES, 0.8, False),
    AnomalyEdge("backup", "resource_bottleneck_io", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("maintenance_conflict", "slow_query", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("maintenance_conflict", "timeout", EdgeRelation.CAUSES, 0.6, False),

    # excessive_index propagation
    AnomalyEdge("excessive_index", "slow_query", EdgeRelation.CAUSES, 0.8, True),

    # resource bottleneck propagation
    AnomalyEdge("resource_cpu", "resource_bottleneck_cpu", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_bottleneck_cpu", "slow_query", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_bottleneck_cpu", "qps_drop", EdgeRelation.CAUSES, 0.9, True),

    AnomalyEdge("resource_io", "resource_bottleneck_io", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_bottleneck_io", "slow_query", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_bottleneck_io", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    AnomalyEdge("resource_memory", "sort_hash_spill", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_memory", "slow_query", EdgeRelation.CAUSES, 0.8, True),

    AnomalyEdge("resource_network", "timeout", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_network", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    # stale_statistics propagation
    AnomalyEdge("stale_statistics", "poor_plan", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("stale_statistics", "slow_query", EdgeRelation.CAUSES, 0.7, False),
]


# ---------------------------------------------------------------------------
# Assemble the graph
# ---------------------------------------------------------------------------

ANOMALY_GRAPH = AnomalyGraph(nodes=NODES, edges=EDGES)


# ---------------------------------------------------------------------------
# Convenience lookups
# ---------------------------------------------------------------------------

def node(node_id: str) -> AnomalyNode | None:
    return ANOMALY_GRAPH.node(node_id)


def injectable_nodes() -> list[AnomalyNode]:
    return ANOMALY_GRAPH.injectable_nodes()


def reachable_paths(source: str, target: str) -> list[list[str]]:
    return ANOMALY_GRAPH.reachable_paths(source, target)


def successors(node_id: str) -> list[str]:
    return ANOMALY_GRAPH.successors(node_id)


def predecessors(node_id: str) -> list[str]:
    return ANOMALY_GRAPH.predecessors(node_id)
