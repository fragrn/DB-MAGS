"""The 20 human-authored propagation chains and their current capabilities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainCase:
    case_id: str
    chain: tuple[str, ...]
    status: str
    reason: str
    root_kind: str | None = None
    risky: bool = False

    @property
    def root_node(self) -> str:
        return self.chain[0]


CASES = [
    ChainCase("01_backup_io_redo_qps", ("backup", "redo_log_flush_stall", "qps_drop"), "runnable", "SQL backup root plus observed downstream nodes", "backup"),
    ChainCase("02_connection_storm_timeout_qps", ("connection_storm", "connection_pressure", "qps_drop"), "runnable", "concurrent SQL root; connection pressure and throughput are observed", "connection_storm"),
    ChainCase("03_deadlock_storm_write_throughput", ("deadlock_storm", "deadlock_detected", "write_throughput_drop"), "skipped", "deadlock injection Action is not implemented", None),
    ChainCase("04_disk_pressure_qps", ("disk_full_or_pressure", "disk_saturation", "slow_query", "qps_drop"), "skipped", "disk injection is disabled on the local host", None, True),
    ChainCase("05_excessive_index_slow_qps", ("excessive_index", "slow_query", "qps_drop"), "runnable", "unindexed filter/sort SQL root", "excessive_index"),
    ChainCase("06_hot_update_lock_slow_qps", ("hot_update", "row_lock", "slow_query", "qps_drop"), "runnable", "concurrent update SQL root", "hot_update"),
    ChainCase("07_improper_sql_spill_slow_qps", ("improper_sql", "sort_hash_spill", "slow_query", "qps_drop"), "runnable", "sort-heavy SQL root; spill is observed", "improper_sql"),
    ChainCase("08_large_temp_table_spill_slow_qps", ("large_temp_table", "temp_table_spill", "slow_query", "qps_drop"), "runnable", "temporary-table SQL root; spill is observed", "large_temp_table"),
    ChainCase("09_long_tx_lock_slow_qps", ("long_tx", "row_lock", "slow_query", "qps_drop"), "runnable", "long transaction root", "long_tx"),
    ChainCase("10_memory_buffer_pool_slow_qps", ("resource_memory", "buffer_pool_pressure", "slow_query", "qps_drop"), "runnable", "ChaosBlade memory root; buffer pool is observed", "resource_memory", True),
    ChainCase("11_metadata_lock_slow_qps", ("metadata_lock", "metadata_lock_wait", "slow_query", "qps_drop"), "runnable", "metadata lock transaction root", "metadata_lock"),
    ChainCase("12_missing_index_plan_slow_qps", ("missing_index", "slow_query", "qps_drop"), "runnable", "unindexed filter/sort SQL root", "missing_index"),
    ChainCase("13_network_latency_qps", ("network_latency", "network_stall", "timeout", "qps_drop"), "skipped", "network injection is disabled on the local host", None, True),
    ChainCase("14_redo_pressure_commit_throughput", ("redo_log_pressure", "redo_log_flush_stall", "commit_latency_up", "write_throughput_drop"), "runnable", "bounded concurrent UPDATE root", "redo_log_pressure"),
    ChainCase("15_cpu_bottleneck_slow_qps", ("resource_cpu", "slow_query", "qps_drop"), "runnable", "ChaosBlade CPU root", "resource_cpu"),
    ChainCase("16_io_bottleneck_slow_qps", ("resource_io", "resource_bottleneck_io", "slow_query", "qps_drop"), "skipped", "disk/IO injection is disabled on the local host", None, True),
    ChainCase("17_memory_slow_qps", ("resource_memory", "slow_query", "qps_drop"), "runnable", "ChaosBlade memory root", "resource_memory", True),
    ChainCase("18_memory_sort_spill_slow_qps", ("resource_memory", "sort_hash_spill", "slow_query", "qps_drop"), "runnable", "ChaosBlade memory root; spill is observed", "resource_memory", True),
    ChainCase("19_table_lock_wait_qps", ("table_lock", "table_blocking", "qps_drop"), "runnable", "table-lock transaction root", "table_lock"),
    ChainCase("20_traffic_threads_lock_slow_qps", ("traffic_surge", "threads_concurrency_up", "row_lock", "slow_query", "qps_drop"), "runnable", "BenchBase TPCC root", "traffic_surge"),
]


CASE_BY_ID = {case.case_id: case for case in CASES}
