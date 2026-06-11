# DBMAGS Experiment Report — 20260611-110304_b5fa81

**Status**: FAILED | Rounds: 2 | Final Score: 0.546

## 1. Experiment Target
- **Anomaly**: traffic_surge_to_qps_drop_on_tpcc
- **Database**: tpcc_10W
- **DBA Description**: Reproduce traffic surge propagation on TPCC with background BenchBase TPCC workload.
- **Target Path**: `traffic_surge -> threads_concurrency_up -> lock_contention -> slow_query -> qps_drop`
- **Injected Nodes**: `traffic_surge`
- **Max Duration**: 30s
- **Risk Level**: medium

## 2. Environment
- **DB Version**: 9.6.0
- **Tables**: 9
- **Threads**: connected=5, running=6
- **Slow Queries**: 46
- **Max Connections**: 151

## 3. Anomaly Propagation Chain
**Target Path**: `traffic_surge -> threads_concurrency_up -> lock_contention -> slow_query -> qps_drop`
**Path Hit**: False
**Node Hit Ratio**: 60.0%
**Failure Stage**: terminal_not_hit:qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `traffic_surge` | YES | 1.00 | Threads_connected ratio_up 1.5 -> matched; Threads_running r |
| `threads_concurrency_up` | YES | 1.00 | Threads_running ratio_up 1.5 -> matched; active_sessions_del |
| `lock_contention` | YES | 0.70 | Innodb_row_lock_waits ratio_up 2.0 -> matched; Innodb_row_lo |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | NO | 0.00 | Required rule not matched: qps_ratio ratio_down 0.7 -> NOT m |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `traffic_surge_burst` | traffic_surge | medium | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `traffic_surge_burst` | completed | 2026-06-11T11:17:24 | 2026-06-11T11:17:41 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 5.0, 'last | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 79.0, 'las | {'first': 79.0, 'las | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 740243.0,  | {'first': 808608.0,  | - |
| db_metrics.Innodb_row_lock_time | {'first': 11633849.0 | {'first': 12389133.0 | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 15.0, 'las | {'first': 15.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3388.8, 'l | {'first': 3551.8, 'l | - |
| workload.tps | {'first': 2165.4, 'l | {'first': 2187.8, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.546 |
| Performance | -0.046 |
| Target Anomaly | 0.600 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 3/5 nodes hit. Failure at: terminal_not_hit:qps_drop |

## 9. Reflection
**Failure Reason**: The surge increased concurrency and produced short lock conflicts, but average lock wait time did not rise enough to push any query past the slow-query threshold. Without slow queries, throughput stayed at or above baseline, so the terminal qps_drop was not hit. This is consistent with prior runs: traffic_surge alone has repeatedly failed to generate slow_query and qps_drop. You likely need stronger, longer, and more write-heavy contention to create sustained blocking, or else add injected_nodes (e.g., slow_sql/lock_holder) if you want deterministic slow-query and QPS collapse.

**Suggested Changes**:
- Increase traffic_surge terminals to 120 (≈12 per warehouse) to amplify concurrent row-level conflicts and raise lock wait times.
- Raise traffic_surge rate to 2800 tx/s to exceed service capacity and induce queueing so that some transactions wait beyond long_query_time.
- Extend traffic_surge duration_sec to 900 to allow contention to build and persist long enough to register slow_query_count_delta ≥ 1.
- Skew transaction_mix to be write-heavy and lock-prone: new_order=0.55, payment=0.20, delivery=0.15, stock_level=0.08, order_status=0.02. The higher Delivery share increases lock hold times; maintaining some Stock-Level helps produce longer reads under contention.
- Use mix_template="tpcc_write_lock_hot" to bias towards high-contention paths; keep rationale documenting the intent to elevate Innodb_row_lock_time_avg and trigger slow queries.
- Operational check: ensure slow query logging is enabled and long_query_time is not set excessively high; otherwise traffic-only changes may still fail to emit slow queries.
- If traffic_surge remains the only injected node, be aware that producing a clear qps_drop from a traffic spike alone can be unreliable. For deterministic reproduction, consider adding injected_nodes like a targeted lock holder or slow SQL in a future run (do not change them in this run).

**Parameter Updates**:
- `traffic_surge`: {"terminals": 120, "rate": 2800, "duration_sec": 900, "transaction_mix": {"new_order": 0.55, "payment": 0.2, "delivery": 0.15, "stock_level": 0.08, "order_status": 0.02}, "mix_template": "tpcc_write_lock_hot", "rationale": "Increase concurrency and write-heavy skew to elevate row lock contention and average lock wait time, pushing some transactions beyond the slow-query threshold and causing throughput collapse (qps_ratio down \u2264 0.7)."}

**Risk Warning**: These changes can cause severe lock contention, long waits (>10s), deadlocks, rollback storms, and QPS collapse. Monitor error rates, deadlocks, InnoDB lock wait metrics, and replication lag. Have safeguards to abort the surge if sustained timeouts or service SLO violations exceed acceptable limits.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 71337
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
