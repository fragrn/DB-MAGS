# DBMAGS Experiment Report — 20260622-065905_c8c033

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
- **Threads**: connected=5, running=5
- **Slow Queries**: 219
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
| `missing_index_order_line_fullscan` | missing_index | This task issues a hot full-table scan on order_line (~13.6M rows) without an index on ol_delivery_d, generating poor plans and latency pressure. Limited to 15s and concurrency 4 to fit the injection window and reduce risk of excessive load. | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `missing_index_order_line_fullscan` | completed | 2026-06-22T07:12:02 | 2026-06-22T07:12:22 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 9.0, 'last | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 7.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 105.0, 'la | {'first': 105.0, 'la | - |
| db_metrics.Slow_queries | {'first': 219.0, 'la | {'first': 219.0, 'la | - |
| db_metrics.Innodb_row_lock_waits | {'first': 2583293.0, | {'first': 2632728.0, | - |
| db_metrics.Innodb_row_lock_time | {'first': 101071260. | {'first': 101748678. | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 39.0, 'las | {'first': 38.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3421.6, 'l | {'first': 1079.2, 'l | - |
| workload.tps | {'first': 2172.2, 'l | {'first': 699.2, 'la | - |
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
**Failure Reason**: QPS/TPS dropped but the chain did not pass through slow_query because the workload never produced a missing-index-driven full table scan. The relevant secondary index stayed available (EXPLAIN did not show type=ALL), the executed queries rarely targeted the non-indexed predicate (rows_examined_ratio never exceeded the threshold), and slow query logging did not register any slow statements. The observed qps_drop likely came from client/load changes or general variability rather than slow queries caused by a poor plan, breaking the slow_query -> qps_drop edge.

**Suggested Changes**:
- Force a true missing-index path on the TPC-C customer last-name lookups: mark the customer name index invisible so the optimizer must scan. SQL: ALTER TABLE tpcc.customer ALTER INDEX idx_customer INVISIBLE; (common schema: idx_customer on (c_w_id,c_d_id,c_last,c_first)). Revert after the run: ALTER TABLE tpcc.customer ALTER INDEX idx_customer VISIBLE;
- If your index name differs (e.g., idx_customer_name or idx_customer_last), adjust the SQL accordingly. You can verify the exact name via: SELECT index_name, column_name FROM information_schema.statistics WHERE table_schema='tpcc' AND table_name='customer' ORDER BY seq_in_index;
- Bias the workload to actually hit the last-name path so EXPLAIN shows type=ALL: set the transaction mix to 100% by-last-name for Payment and OrderStatus during the chaos window. Example TaskSpec changes for your loadgen: transaction_mix.payment_by_lastname_pct=100; transaction_mix.orderstatus_by_lastname_pct=100; duration_sec=600;
- If altering the index is not possible, force a poor plan via index hints in the transaction SQL. For the customer lookup by last name, modify the query to: SELECT ... FROM tpcc.customer IGNORE INDEX (idx_customer) WHERE c_w_id=? AND c_d_id=? AND c_last=?; This ensures explain_access_type=ALL while leaving the index intact.
- Increase offered load enough to manifest slow queries and backpressure while keeping it constant so throughput drops only due to longer latencies: set terminals (concurrency) to at least 2x your baseline (e.g., terminals=64) and hold a steady open-loop request rate if your driver supports it (rate_per_sec fixed), do not throttle down mid-run.
- Enable and sensitize slow query logging so slow_query_count_delta >= 1 will trigger: SQL to run before the test: SET GLOBAL slow_query_log=ON; SET GLOBAL long_query_time=0.05; SET GLOBAL log_output='TABLE'; SET GLOBAL log_queries_not_using_indexes=ON; (revert long_query_time to your baseline after).
- Validate before measuring: run EXPLAIN on the by-last-name query and confirm type=ALL and key=NULL; then run a short probe (30–60s) to confirm rows_examined grows 100x+ vs rows_returned (rows_examined_ratio > 2).
- Do not combine CPU/IO/network chaos with this anomaly while validating the causal chain. External resource faults can cause qps_drop without creating slow_query entries, masking the intended propagation.
- Extend the measurement window to let slow logs accumulate and the detection rules settle (duration_sec >= 300).
- Optional (stronger effect): if the above is too mild, also mark the orders customer index invisible to force a scan on OrderStatus last order lookup. SQL: ALTER TABLE tpcc.orders ALTER INDEX idx_orders_cust INVISIBLE; Revert to VISIBLE after the run.

**Parameter Updates**:
- `missing_index`: {"table": "tpcc.customer", "index": "idx_customer", "sql_text": "ALTER TABLE tpcc.customer ALTER INDEX idx_customer INVISIBLE", "predicate": "WHERE c_w_id=? AND c_d_id=? AND c_last=?"}

**Risk Warning**: Making secondary indexes INVISIBLE (or dropping them) will degrade Payment and OrderStatus performance cluster-wide and can cause long-running transactions and timeouts. Changing MySQL globals (slow_query_log, long_query_time) affects the entire instance and may increase logging overhead. Perform these steps only in a non-production environment. If you must run on production-like datasets, prefer ALTER INDEX ... INVISIBLE (instant metadata change) over DROP INDEX to avoid long, blocking DDL. Ensure you revert indexes to VISIBLE and restore long_query_time after the experiment.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 80665
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
