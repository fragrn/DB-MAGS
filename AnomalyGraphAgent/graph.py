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
        injectable=False,
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
        planner_notes="Use chaosblade disk burn IO stress for the configured duration.",
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

    "metadata_lock": AnomalyNode(
        node_id="metadata_lock",
        label="Metadata Lock",
        description="DDL or table metadata operation blocks concurrent SQL through MDL waits.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_lock_task",
        planner_notes=(
            "Use raw_transaction_script or raw_sql_workload to hold a transaction on a hot table, "
            "then run bounded ALTER TABLE/LOCK TABLE/DDL that creates Waiting for table metadata lock. "
            "Keep statements reversible and scoped to the target database."
        ),
        evidence_rules=[
            _r("metadata_lock_evidence", "exists", True, required=True, weight=2.0),
            _r("metadata_lock_wait_count", ">", 0, required=False, weight=1.0),
        ],
    ),

    "table_lock": AnomalyNode(
        node_id="table_lock",
        label="Table Lock",
        description="Explicit table lock or table-level operation blocks concurrent access.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_lock_task",
        planner_notes=(
            "Use raw_transaction_script with LOCK TABLES or a table-level write operation, "
            "then run concurrent readers/writers against the same table. Always UNLOCK TABLES in cleanup."
        ),
        evidence_rules=[
            _r("lock_wait_time_delta", ">", 0, required=True, weight=1.5),
            _r("blocked_session_count", ">", 0, required=False, weight=1.0),
        ],
    ),

    "network_latency": AnomalyNode(
        node_id="network_latency",
        label="Network Latency",
        description="Network delay/drop between client and MySQL increases request latency and timeouts.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes=(
            "Use ChaosBlade raw_command network delay/drop scoped to MySQL traffic when supported. "
            "Generate a unique --uid and matching cleanup_command."
        ),
        evidence_rules=[
            _r("network_error_delta", ">", 0, required=False, weight=1.0),
            _r("connection_error_delta", ">", 0, required=False, weight=1.0),
            _r("qps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
        ],
    ),

    "disk_full_or_pressure": AnomalyNode(
        node_id="disk_full_or_pressure",
        label="Disk Full or Disk Pressure",
        description="Controlled disk pressure or near-full condition slows reads/writes.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_chaos_task",
        planner_notes=(
            "Use controlled ChaosBlade disk burn/fill or bounded writes under /tmp/dbmags. "
            "Do not attempt to fill the system disk; always include cleanup."
        ),
        evidence_rules=[
            _r("io_wait_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("disk_util", ">", 0.8, required=False, weight=1.0),
        ],
    ),

    "deadlock_storm": AnomalyNode(
        node_id="deadlock_storm",
        label="Deadlock Storm",
        description="Repeated conflicting transactions create InnoDB deadlocks.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_lock_task",
        planner_notes=(
            "Use raw_transaction_script with two roles updating the same rows in opposite order. "
            "Run several concurrent pairs within the observation window."
        ),
        evidence_rules=[
            _r("Innodb_deadlocks", ">", 0, required=True, weight=2.0),
            _r("deadlock_delta", ">", 0, required=False, weight=1.0),
        ],
    ),

    "large_temp_table": AnomalyNode(
        node_id="large_temp_table",
        label="Large Temporary Table",
        description="Large ORDER BY/GROUP BY/JOIN workload creates temporary tables and disk spills.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_slow_sql_task",
        planner_notes=(
            "Use raw_sql_workload with GROUP BY, ORDER BY, DISTINCT, or JOIN over large tables. "
            "Prefer SQL that EXPLAIN shows Using temporary or Using filesort."
        ),
        evidence_rules=[
            _r("Created_tmp_disk_tables_delta", ">", 0, required=True, weight=2.0),
            _r("Sort_merge_passes_delta", ">", 0, required=False, weight=1.0),
        ],
    ),

    "redo_log_pressure": AnomalyNode(
        node_id="redo_log_pressure",
        label="Redo Log Pressure",
        description="High-concurrency writes create redo/fsync pressure and commit latency.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_slow_sql_task",
        planner_notes=(
            "Use raw_sql_workload or raw_transaction_script with high-concurrency INSERT/UPDATE/COMMIT. "
            "Keep writes scoped and reversible where possible."
        ),
        evidence_rules=[
            _r("write_latency_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("Innodb_log_waits_delta", ">", 0, required=False, weight=1.0),
            _r("tps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
        ],
    ),

    "connection_storm": AnomalyNode(
        node_id="connection_storm",
        label="Connection Storm",
        description="Large bursts of short-lived or concurrent connections exhaust connection capacity.",
        category=NodeCategory.INJECTABLE,
        injectable=True,
        default_tool="build_traffic_task",
        planner_notes=(
            "Use raw_sql_workload or command/script workload that opens many bounded MySQL connections. "
            "Prefer short connection bursts over long-running idle sessions."
        ),
        evidence_rules=[
            _r("Threads_connected", "ratio_up", 1.5, required=True, weight=1.5),
            _r("connection_error_delta", ">", 0, required=False, weight=1.0),
            _r("Max_used_connections", "ratio_up", 1.2, required=False, weight=0.8),
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

    "metadata_lock_wait": AnomalyNode(
        node_id="metadata_lock_wait",
        label="Metadata Lock Wait",
        description="Sessions are waiting for table metadata locks.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("metadata_lock_evidence", "exists", True, required=True, weight=2.0),
            _r("metadata_lock_wait_count", ">", 0, required=False, weight=1.0),
        ],
    ),

    "connection_pressure": AnomalyNode(
        node_id="connection_pressure",
        label="Connection Pressure",
        description="Connection usage approaches limits or connection errors increase.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Threads_connected", "ratio_up", 1.5, required=True, weight=1.5),
            _r("connection_error_delta", ">", 0, required=False, weight=1.0),
            _r("active_sessions_delta", ">", 5, required=False, weight=0.8),
        ],
    ),

    "temp_table_spill": AnomalyNode(
        node_id="temp_table_spill",
        label="Temporary Table Spill",
        description="Temporary tables or sort operations spill to disk.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Created_tmp_disk_tables_delta", ">", 0, required=True, weight=2.0),
            _r("Sort_merge_passes_delta", ">", 0, required=False, weight=1.0),
        ],
    ),

    "buffer_pool_pressure": AnomalyNode(
        node_id="buffer_pool_pressure",
        label="Buffer Pool Pressure",
        description="Buffer pool misses, reads, or dirty page pressure increases.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("buffer_pool_read_ratio", "ratio_up", 1.3, required=False, weight=1.0),
            _r("Innodb_buffer_pool_reads_delta", ">", 0, required=False, weight=1.0),
            _r("memory_usage_ratio", "ratio_up", 1.2, required=False, weight=0.8),
        ],
    ),

    "redo_log_flush_stall": AnomalyNode(
        node_id="redo_log_flush_stall",
        label="Redo Log Flush Stall",
        description="Redo log flushing or commit fsync pressure slows writes.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Innodb_log_waits_delta", ">", 0, required=False, weight=1.0),
            _r("write_latency_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("tps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
        ],
    ),

    "binlog_flush_stall": AnomalyNode(
        node_id="binlog_flush_stall",
        label="Binlog Flush Stall",
        description="Binary log flushing or group commit stalls write commits.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("Binlog_cache_disk_use_delta", ">", 0, required=False, weight=1.0),
            _r("write_latency_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("tps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
        ],
    ),

    "deadlock_detected": AnomalyNode(
        node_id="deadlock_detected",
        label="Deadlock Detected",
        description="InnoDB deadlock counter increased during injection.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("deadlock_delta", ">", 0, required=True, weight=2.0),
            _r("Innodb_deadlocks", ">", 0, required=False, weight=1.0),
        ],
    ),

    "disk_saturation": AnomalyNode(
        node_id="disk_saturation",
        label="Disk Saturation",
        description="Disk utilization, IO wait, or disk-backed temp activity increases.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("io_wait_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("disk_util", ">", 0.8, required=False, weight=1.0),
            _r("Created_tmp_disk_tables_delta", ">", 0, required=False, weight=0.8),
        ],
    ),

    "network_stall": AnomalyNode(
        node_id="network_stall",
        label="Network Stall",
        description="Network latency or packet loss causes connection errors or throughput loss.",
        category=NodeCategory.INTERMEDIATE,
        injectable=False,
        evidence_rules=[
            _r("network_error_delta", ">", 0, required=False, weight=1.0),
            _r("connection_error_delta", ">", 0, required=False, weight=1.0),
            _r("qps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
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
            _r("slow_log_target_entry_count", ">=", 1, required=True, weight=1.0),
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
            _r("qps_ratio", "ratio_down", 0.7, required=True, weight=1.0),
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

    "commit_latency_up": AnomalyNode(
        node_id="commit_latency_up",
        label="Commit Latency Up",
        description="Write transaction commit latency increases.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("write_latency_ratio", "ratio_up", 1.5, required=False, weight=1.0),
            _r("tps_ratio", "ratio_down", 0.8, required=False, weight=0.8),
        ],
    ),

    "connection_error": AnomalyNode(
        node_id="connection_error",
        label="Connection Error",
        description="Connection failures, aborted connects, or client timeout errors increase.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("connection_error_delta", ">", 0, required=True, weight=2.0),
            _r("Aborted_connects", "ratio_up", 1.5, required=False, weight=1.0),
        ],
    ),

    "write_throughput_drop": AnomalyNode(
        node_id="write_throughput_drop",
        label="Write Throughput Drop",
        description="Write TPS decreases significantly while write workload is active.",
        category=NodeCategory.SYMPTOM,
        injectable=False,
        evidence_rules=[
            _r("tps_ratio", "ratio_down", 0.7, required=True, weight=1.5),
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
    AnomalyEdge("improper_sql", "temp_table_spill", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("sort_hash_spill", "slow_query", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("temp_table_spill", "slow_query", EdgeRelation.CAUSES, 0.9, True),
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
    AnomalyEdge("resource_bottleneck_io", "redo_log_flush_stall", EdgeRelation.AMPLIFIES, 0.7, False),
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
    AnomalyEdge("resource_memory", "buffer_pool_pressure", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("buffer_pool_pressure", "slow_query", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("resource_memory", "slow_query", EdgeRelation.CAUSES, 0.8, True),

    AnomalyEdge("resource_network", "timeout", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("resource_network", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    # stale_statistics propagation
    AnomalyEdge("stale_statistics", "poor_plan", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("stale_statistics", "slow_query", EdgeRelation.CAUSES, 0.7, False),

    # expanded common DBA propagation chains
    AnomalyEdge("metadata_lock", "metadata_lock_wait", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("metadata_lock_wait", "slow_query", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("metadata_lock_wait", "timeout", EdgeRelation.CAUSES, 0.8, False),

    AnomalyEdge("table_lock", "lock_contention", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("lock_contention", "lock_wait", EdgeRelation.CAUSES, 0.9, False),
    AnomalyEdge("lock_wait", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    AnomalyEdge("deadlock_storm", "deadlock_detected", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("deadlock_detected", "deadlock", EdgeRelation.CAUSES, 1.0, False),
    AnomalyEdge("deadlock_detected", "write_throughput_drop", EdgeRelation.CAUSES, 0.8, True),

    AnomalyEdge("connection_storm", "connection_pressure", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("connection_pressure", "timeout", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("connection_pressure", "connection_error", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("timeout", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    AnomalyEdge("network_latency", "network_stall", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("network_stall", "timeout", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("network_stall", "qps_drop", EdgeRelation.CAUSES, 0.8, False),

    AnomalyEdge("large_temp_table", "temp_table_spill", EdgeRelation.CAUSES, 1.0, True),

    AnomalyEdge("redo_log_pressure", "redo_log_flush_stall", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("redo_log_flush_stall", "commit_latency_up", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("redo_log_flush_stall", "qps_drop", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("binlog_flush_stall", "commit_latency_up", EdgeRelation.CAUSES, 0.8, False),
    AnomalyEdge("commit_latency_up", "write_throughput_drop", EdgeRelation.CAUSES, 0.9, True),

    AnomalyEdge("disk_full_or_pressure", "disk_saturation", EdgeRelation.CAUSES, 1.0, True),
    AnomalyEdge("disk_saturation", "slow_query", EdgeRelation.CAUSES, 0.9, True),
    AnomalyEdge("disk_saturation", "redo_log_flush_stall", EdgeRelation.AMPLIFIES, 0.7, False),
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
