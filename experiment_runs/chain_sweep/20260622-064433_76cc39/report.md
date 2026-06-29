# DBMAGS Experiment Report — 20260622-064433_76cc39

**Status**: FAILED | Rounds: 2 | Final Score: 0.650

## 1. Experiment Target
- **Anomaly**: long_tx_lock_contention_chain
- **Database**: tpcc_10W
- **DBA Description**: Generate a long transaction task to induce lock contention on TPCC background workload.
- **Target Path**: `long_tx -> lock_contention -> slow_query -> qps_drop`
- **Injected Nodes**: `long_tx`
- **Max Duration**: 30s
- **Risk Level**: medium

## 2. Environment
- **DB Version**: 9.6.0
- **Tables**: 9
- **Threads**: connected=5, running=4
- **Slow Queries**: 203
- **Max Connections**: 151

## 3. Anomaly Propagation Chain
**Target Path**: `long_tx -> lock_contention -> slow_query -> qps_drop`
**Path Hit**: False
**Node Hit Ratio**: 0.0%
**Failure Stage**: terminal_not_hit:qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `long_tx` | NO | 0.00 | Required rule not matched: Innodb_row_lock_time ratio_up 2.0 |
| `lock_contention` | NO | 0.00 | Required rule not matched: Innodb_row_lock_waits ratio_up 2. |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | NO | 0.00 | Required rule not matched: qps_ratio ratio_down 0.7 -> NOT m |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `long_tx_tpcc_lock_hold_w_shard` | anomaly_injection | Holds row and next-key locks on hot TPC-C rows (district and stock) across up to 4 warehouses for ~12s. Expect lock waits, slow queries, possible deadlocks. Use only in non-production. | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `long_tx_tpcc_lock_hold_w_shard` | completed | 2026-06-22T06:57:24 | 2026-06-22T06:57:24 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 5.0, 'last | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 105.0, 'la | {'first': 105.0, 'la | - |
| db_metrics.Slow_queries | {'first': 203.0, 'la | {'first': 203.0, 'la | - |
| db_metrics.Innodb_row_lock_waits | {'first': 2419139.0, | {'first': 2480069.0, | - |
| db_metrics.Innodb_row_lock_time | {'first': 99100412.0 | {'first': 99799675.0 | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 40.0, 'las | {'first': 40.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3531.6, 'l | {'first': 2130.0, 'l | - |
| workload.tps | {'first': 2240.6, 'l | {'first': 1384.0, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.650 |
| Performance | 1.000 |
| Target Anomaly | 0.000 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 0/4 nodes hit. Failure at: terminal_not_hit:qps_drop |

## 9. Reflection
**Failure Reason**: The long_tx did not hold exclusive locks on hot TPCC rows that the live workload contends for, so no blocking or waits accumulated. Likely issues include: locking the wrong table/keys (e.g., low-traffic rows), autocommit or isolation incorrectly releasing locks, lock duration too short, contention generator not targeting the same keys or too low concurrency, and slow query logging thresholds too high to count waits. With uniform TPCC_10W load, locking a single cold row won’t move Innodb_row_lock_time, slow_query_count, or QPS.

**Suggested Changes**:
- Retarget the long transaction to TPCC hotspots: lock tpcc.district rows (d_w_id, d_id) that every New-Order updates. Lock all 10 districts in 1–2 warehouses (e.g., w_id IN (1,2), d_id 1..10) to guarantee broad contention.
- Ensure the lock is truly held: disable autocommit, set isolation to REPEATABLE READ, use SELECT ... FOR UPDATE on the district rows, and hold the transaction open for at least 120–150 seconds before committing. Start it a few seconds before contention traffic so it acquires locks first.
- Drive contention with high concurrency against the SAME keys: flood UPDATE tpcc.district SET d_next_o_id = d_next_o_id WHERE d_w_id IN (1,2) AND d_id BETWEEN 1 AND 10 using 32–64 concurrent sessions and minimal think time for the full lock duration.
- Increase per-session wait and slow thresholds in the contention clients so waits are recorded as slow queries rather than quick timeouts: SET SESSION innodb_lock_wait_timeout=120 and SET SESSION long_query_time=1 in the contention sessions.
- Align timings: start lock_contention 5–10 seconds after long_tx begins, and run for at least 120 seconds to accumulate Innodb_row_lock_time and Innodb_row_lock_waits.
- If your TPCC driver is external and uniformly spreads load across 10 warehouses, skew it to warehouses 1–2 or increase terminals so that blocking a subset of districts still drops overall QPS >30%. If you do not have a workload control node, you will need to add an injected node to adjust terminals/skew.
- Verify slow query logging is enabled (slow_query_log=ON) and long_query_time <= 1 globally or per-session for the contention clients. If you cannot set this via existing tasks, you need to add an injected node to apply these settings.
- Extend total experiment duration to at least 3–5 minutes so metrics windows capture ratio_up/down conditions.

**Parameter Updates**:
- `long_tx`: {"table": "tpcc.district", "predicate": "d_w_id IN (1,2) AND d_id BETWEEN 1 AND 10", "sql_text": "SET SESSION autocommit=0; SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; START TRANSACTION; SELECT d_next_o_id FROM tpcc.district WHERE d_w_id IN (1,2) AND d_id BETWEEN 1 AND 10 FOR UPDATE; DO SLEEP(150); COMMIT;", "duration_sec": 150, "start_offset_sec": 10, "concurrency": 1}
- `lock_contention`: {"sql_text": "UPDATE tpcc.district SET d_next_o_id = d_next_o_id WHERE d_w_id IN (1,2) AND d_id BETWEEN 1 AND 10", "session_sql_preamble": "SET SESSION innodb_lock_wait_timeout=120; SET SESSION long_query_time=1", "concurrency": 64, "rate": "unlimited", "duration_sec": 130, "start_offset_sec": 20, "error_policy": "continue_on_timeout"}

**Risk Warning**: This will intentionally stall New-Order/Payment on targeted warehouses/districts, causing widespread lock waits, slow queries, and a sharp QPS drop. Expect timeouts, deadlocks, and potential connection pool saturation. Ensure slow query logging is enabled and log volume is acceptable. Run only in non-production or with strict blast radius (limit to specific warehouses) and monitor replication lag if using replicas.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 67673
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
