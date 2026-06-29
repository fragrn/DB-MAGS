# DBMAGS Experiment Report — 20260621-094223_c6441d

**Status**: FAILED | Rounds: 2 | Final Score: 0.511

## 1. Experiment Target
- **Anomaly**: hot_update_lock_contention_chain
- **Database**: tpcc_10W
- **DBA Description**: Generate a hot update task to induce lock contention on TPCC background workload.
- **Target Path**: `hot_update -> lock_contention -> slow_query -> qps_drop`
- **Injected Nodes**: `hot_update`
- **Max Duration**: 30s
- **Risk Level**: medium

## 2. Environment
- **DB Version**: 9.6.0
- **Tables**: 9
- **Threads**: connected=5, running=4
- **Slow Queries**: 46
- **Max Connections**: 151

## 3. Anomaly Propagation Chain
**Target Path**: `hot_update -> lock_contention -> slow_query -> qps_drop`
**Path Hit**: False
**Node Hit Ratio**: 50.0%
**Failure Stage**: terminal_not_hit:qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `hot_update` | YES | 0.70 | Innodb_row_lock_waits ratio_up 2.0 -> matched; Innodb_row_lo |
| `lock_contention` | YES | 0.70 | Innodb_row_lock_waits ratio_up 2.0 -> matched; Innodb_row_lo |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | NO | 0.00 | Required rule not matched: qps_ratio ratio_down 0.7 -> NOT m |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `hot_update_tpcc_district_lock` | injection | ['High lock contention on tpcc_10W.district(d_w_id=1,d_id=1) may stall TPCC transactions touching this district', 'Potential deadlocks and long lock waits; monitor Innodb_deadlocks and lock wait time', 'Duration 15s and 64 concurrent workers aim for impact within observe window; under max_duration_sec=30'] | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `hot_update_tpcc_district_lock` | completed | 2026-06-21T09:53:34 | 2026-06-21T09:53:34 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 5.0, 'last | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 105.0, 'la | {'first': 105.0, 'la | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 1440296.0, | {'first': 1473589.0, | - |
| db_metrics.Innodb_row_lock_time | {'first': 21081540.0 | {'first': 21432924.0 | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 14.0, 'las | {'first': 14.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 2694.6, 'l | {'first': 2837.0, 'l | - |
| workload.tps | {'first': 1719.8, 'l | {'first': 1833.6, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.511 |
| Performance | -0.045 |
| Target Anomaly | 0.500 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 2/4 nodes hit. Failure at: terminal_not_hit:qps_drop |

## 9. Reflection
**Failure Reason**: Contention was created but not sustained on the TPCC critical path. The hot updates likely locked too many distinct keys (diluting wait time) and/or used short autocommit updates that released locks quickly. As a result, Innodb_row_lock_time_avg did not rise enough, slow queries did not register (no waits exceeding slow threshold), and the overall workload QPS did not drop. To hit slow_query and qps_drop, you need fewer, truly hot keys on a TPCC-critical row and hold the lock longer within explicit transactions so other sessions block for >1s. If QPS_drop still does not trigger, the current set of injected_nodes may be insufficient to depress cluster-wide QPS and you may need to add more coverage with additional injected hot_update(s) targeting multiple warehouses.

**Suggested Changes**:
- Concentrate contention on TPCC-critical rows: target tpcc.district (PK: d_w_id, d_id) which NewOrder updates every time. Lock d_id=1 for multiple warehouses rather than many disparate rows.
- Hold the lock explicitly: run BEGIN; SELECT ... FOR UPDATE; then SELECT SLEEP(2); before COMMIT. This guarantees >2s waits for contending updates, raising Innodb_row_lock_time_avg and creating slow queries.
- Increase hot_update concurrency to 80–128 threads, but restrict hot keys to a small set (e.g., 5 warehouses: w_id in (1..5), d_id=1). This creates a deep lock wait queue and increases average wait time without diffusing contention.
- Extend run duration to >=180s so metrics aggregators capture enough slow queries and QPS impact.
- Prevent premature timeouts and ensure slow query attribution: set SESSION innodb_lock_wait_timeout=50 and SESSION long_query_time=0.5 within the hot_update connections.
- Disable autocommit and use explicit transactions to keep the row locked while sleeping.
- If the QPS drop still does not reach the 0.7 ratio, you likely need to add additional injected_nodes (for example, another hot_update covering more warehouses) to broaden impact; the current single hot_update may not depress overall QPS enough on tpcc_10W.

**Parameter Updates**:
- `hot_update`: {"concurrency": 80, "duration_sec": 180, "table": "tpcc.district", "target_keys_note": "Target a small hot set: d_id=1 and d_w_id in (1,2,3,4,5). Each worker selects one of these warehouses uniformly.", "transaction_steps": ["SET SESSION autocommit=0", "SET SESSION innodb_lock_wait_timeout=50", "SET SESSION long_query_time=0.5", "BEGIN", "/* choose w_id from {1,2,3,4,5} per iteration */", "SELECT d_next_o_id FROM tpcc.district WHERE d_w_id = ${w_id} AND d_id = 1 FOR UPDATE", "SELECT SLEEP(2.0)", "UPDATE tpcc.district SET d_ytd = d_ytd /* no-op update to retain exclusive lock */ WHERE d_w_id = ${w_id} AND d_id = 1", "COMMIT"], "terminals": 80, "hot_keys": [{"d_w_id": 1, "d_id": 1}, {"d_w_id": 2, "d_id": 1}, {"d_w_id": 3, "d_id": 1}, {"d_w_id": 4, "d_id": 1}, {"d_w_id": 5, "d_id": 1}]}

**Risk Warning**: These changes intentionally create long-held row locks on tpcc.district, which will stall NewOrder transactions for the targeted warehouses. Expect slow queries, possible lock wait timeouts if other clients use low timeouts, deadlock detector activity, thread pool saturation, and a noticeable QPS drop. Ensure you can tolerate a temporary throughput reduction and monitor for replication lag if any. Keep the duration bounded (180s) and be ready to abort the task to release locks if the system becomes unstable.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 93572
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
