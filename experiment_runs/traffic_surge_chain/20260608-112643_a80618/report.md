# DBMAGS Experiment Report — 20260608-112643_a80618

**Status**: FAILED | Rounds: 2 | Final Score: 0.620

## 1. Experiment Target
- **Anomaly**: traffic_surge_to_qps_drop_on_tpcc
- **Database**: tpcc_10W
- **DBA Description**: Reproduce traffic surge propagation on TPCC with background BenchBase TPCC workload.
- **Target Path**: `traffic_surge -> threads_concurrency_up -> lock_contention -> slow_query -> qps_drop`
- **Injected Nodes**: `traffic_surge`
- **Max Duration**: 120s
- **Risk Level**: medium

## 2. Environment
- **DB Version**: 9.6.0
- **Tables**: 9
- **Threads**: connected=17, running=18
- **Slow Queries**: 46
- **Max Connections**: 151

## 3. Anomaly Propagation Chain
**Target Path**: `traffic_surge -> threads_concurrency_up -> lock_contention -> slow_query -> qps_drop`
**Path Hit**: False
**Node Hit Ratio**: 20.0%
**Broken Edge**: `slow_query -> qps_drop`
**Failure Stage**: broken_edge:slow_query->qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `traffic_surge` | NO | 0.00 | Required rule not matched: Threads_connected ratio_up 1.5 -> |
| `threads_concurrency_up` | NO | 0.00 | Required rule not matched: Threads_running ratio_up 1.5 -> N |
| `lock_contention` | NO | 0.00 | Required rule not matched: Innodb_row_lock_waits ratio_up 2. |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | YES | 1.00 | qps_ratio ratio_down 0.7 -> matched; tps_ratio ratio_down 0. |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `traffic_surge_tpcc_10W_v3` | traffic_surge | medium | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `traffic_surge_tpcc_10W_v3` | failed | 2026-06-08T11:40:40 | 2026-06-08T11:40:40 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 17.0, 'las | {'first': 1.0, 'last | - |
| db_metrics.Threads_running | {'first': 17.0, 'las | {'first': 2.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 76.0, 'las | {'first': 76.0, 'las | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 65369.0, ' | {'first': 110896.0,  | - |
| db_metrics.Innodb_row_lock_time | {'first': 2949217.0, | {'first': 4840827.0, | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 45.0, 'las | {'first': 43.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3707.4, 'l | {'first': 0.0, 'last | - |
| workload.tps | {'first': 2363.8, 'l | {'first': 0.0, 'last | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.620 |
| Performance | 1.000 |
| Target Anomaly | 0.200 |
| Causal Order | 1.000 |
| Stability | 0.000 |
| Reason | Propagation chain NOT fully reproduced. 1/5 nodes hit. Failure at: broken_edge:slow_query->qps_drop |

## 9. Reflection
**Failure Reason**: Propagation chain NOT fully reproduced. 1/5 nodes hit. Failure at: broken_edge:slow_query->qps_drop

**Suggested Changes**:
- Increase target_connections or add more ramp stages. Try target_connections at 60% of max_connections instead of 50%.
- Prior nodes need to produce more concurrent access before this node. Increase traffic_surge concurrency or ensure holder/waiter concurrency is high enough.
- The upstream nodes are not producing sufficient query pressure. Increase concurrency or duration of prior tasks.
- Edge slow_query -> qps_drop did not propagate. Increase slow_query intensity or add an intermediate task.

**Parameter Updates**:
- `traffic_surge`: {"target_connections": "increase_by_20pct"}
- `lock_contention`: {"holder_concurrency": "increase_by_1", "waiter_concurrency": "increase_by_4"}

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 50141
- **Running at End**: False
- **Exit Code**: 0

| Phase | Samples |
|-------|---------|
| baseline | 6 |
| injection | 12 |
| recovery | 6 |
