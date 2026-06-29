# DBMAGS Experiment Report — 20260621-101106_420f44

**Status**: FAILED | Rounds: 2 | Final Score: 0.737

## 1. Experiment Target
- **Anomaly**: missing_index_to_qps_drop
- **Database**: tpcc_10W
- **DBA Description**: Generate a missing-index slow SQL task on TPCC background workload.
- **Target Path**: `missing_index -> poor_plan -> slow_query -> qps_drop`
- **Injected Nodes**: `missing_index`
- **Max Duration**: 30s
- **Risk Level**: medium

## 2. Environment
- **DB Version**: 9.6.0
- **Tables**: 9
- **Threads**: connected=5, running=6
- **Slow Queries**: 46
- **Max Connections**: 151

## 3. Anomaly Propagation Chain
**Target Path**: `missing_index -> poor_plan -> slow_query -> qps_drop`
**Path Hit**: False
**Node Hit Ratio**: 25.0%
**Broken Edge**: `slow_query -> qps_drop`
**Failure Stage**: broken_edge:slow_query->qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `missing_index` | NO | 0.00 | Required rule not matched: rows_examined_ratio > 2.0 -> NOT  |
| `poor_plan` | NO | 0.00 | Required rule not matched: explain_access_type contains ALL  |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | YES | 1.00 | qps_ratio ratio_down 0.7 -> matched; tps_ratio ratio_down 0. |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `missing_index_ol_scan` | injection | Full scans on order_line (~9.5M rows) at concurrency 6 may cause IO and CPU spikes and increase lock waits for TPCC writers. Monitor Threads_running and abort if saturation causes errors. | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `missing_index_ol_scan` | completed | 2026-06-21T10:21:15 | 2026-06-21T10:21:33 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 11.0, 'las | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 11.0, 'las | - |
| db_metrics.Max_used_connections | {'first': 105.0, 'la | {'first': 105.0, 'la | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 1779610.0, | {'first': 1831695.0, | - |
| db_metrics.Innodb_row_lock_time | {'first': 24608450.0 | {'first': 25165175.0 | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 13.0, 'las | {'first': 13.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3407.6, 'l | {'first': 1630.6, 'l | - |
| workload.tps | {'first': 2179.0, 'l | {'first': 1019.8, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.737 |
| Performance | 1.000 |
| Target Anomaly | 0.250 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 1/4 nodes hit. Failure at: broken_edge:slow_query->qps_drop |

## 9. Reflection
**Failure Reason**: Upstream conditions for missing_index, poor_plan, and slow_query were not met. The dropped/hidden index did not meaningfully alter the access path of the dominant TPC-C queries (optimizer still used the PK or another secondary index), so rows_examined_ratio stayed <= 2 and EXPLAIN did not show ALL. Additionally, the slow query log likely did not capture any queries (long_query_time too high and/or not enough affected queries executed), so slow_query_count_delta stayed 0. Meanwhile, QPS/TPS dropped for reasons unrelated to slow_query, breaking the slow_query->qps_drop edge. If only the missing_index node was injected without an accompanying workload amplification or slow-log configuration step, the current injected_nodes are insufficient to reliably propagate the chain.

**Suggested Changes**:
- Retarget the missing_index to a secondary index that TPC-C truly relies on for customer lookups by customer-id in orders, then execute a probe query that cannot use any remaining index so EXPLAIN shows access type ALL. Concretely, drop the orders customer index and hammer a predicate on o_c_id only.
- Lower the slow log threshold and enable logging for queries not using indexes before traffic to ensure slow_query_count_delta >= 1 (e.g., SET GLOBAL slow_query_log=ON; SET GLOBAL long_query_time=0.2; SET GLOBAL log_queries_not_using_indexes=ON).
- Within the missing_index node, add a high-frequency scan probe to force poor_plan and slow_query: repeatedly run SELECT COUNT(*) FROM orders WHERE o_c_id = ? with random o_c_id while the index is dropped. This will produce EXPLAIN access type ALL and large rows examined.
- Increase the probe’s concurrency/rate and duration so rows_examined_ratio > 2.0 and at least one slow query is recorded (e.g., concurrency 24–32, rate 300–500 qps, duration 180s).
- Keep the baseline TPC-C workload rate/terminals constant during the injection so that any QPS/TPS drop is due to the induced slow scans, not due to lowered load. If you lack control of the workload as an injected node, you will need to add a workload control node in future runs.
- Verify the actual index name in your tpcc_10W schema before dropping (SHOW INDEX FROM orders) and adjust the DROP INDEX accordingly. After the experiment, restore the index with the exact column list.
- Validate the access path before ramping load: EXPLAIN SELECT COUNT(*) FROM orders WHERE o_c_id=1234 must show type=ALL; if not, choose a different probe predicate that avoids leading indexed columns.

**Parameter Updates**:
- `missing_index`: {"table": "orders", "index_name": "idx_orders_cust", "drop_index_sql": "ALTER TABLE orders DROP INDEX idx_orders_cust", "pre_sql": ["SET GLOBAL slow_query_log = ON", "SET GLOBAL long_query_time = 0.2", "SET GLOBAL log_queries_not_using_indexes = ON"], "verify_explain_sql": "EXPLAIN SELECT COUNT(*) FROM orders WHERE o_c_id = 1234", "probe_sql": "SELECT COUNT(*) FROM orders WHERE o_c_id = ?", "probe_params": {"o_c_id_min": 1, "o_c_id_max": 3000, "random": true}, "probe_concurrency": 24, "probe_rate": 300, "duration_sec": 180, "post_sql": ["ALTER TABLE orders ADD INDEX idx_orders_cust (o_w_id, o_d_id, o_c_id, o_id)", "SET GLOBAL long_query_time = 10", "SET GLOBAL log_queries_not_using_indexes = OFF"]}

**Risk Warning**: Dropping a secondary index on a large orders table can take time and acquire metadata locks; on replicas it may cause replication lag. Enabling slow_query_log and lowering long_query_time will increase log volume. The proposed COUNT(*) scans at high concurrency will stress CPU and I/O, potentially impacting co-located services. Run only in an isolated test environment and restore the index and slow log settings immediately after the experiment.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 9248
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
