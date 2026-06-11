# DBMAGS Experiment Report — 20260609-083057_0c4a9b

**Status**: FAILED | Rounds: 2 | Final Score: 0.427

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
**Node Hit Ratio**: 20.0%
**Failure Stage**: terminal_not_hit:qps_drop

### Node Results
| Node | Hit | Confidence | Evidence |
|------|-----|------------|----------|
| `traffic_surge` | YES | 0.70 | Threads_connected ratio_up 1.5 -> matched; Threads_running r |
| `threads_concurrency_up` | NO | 0.00 | Required rule not matched: Threads_running ratio_up 1.5 -> N |
| `lock_contention` | NO | 0.00 | Required rule not matched: Innodb_row_lock_waits ratio_up 2. |
| `slow_query` | NO | 0.00 | Required rule not matched: slow_query_count_delta >= 1 -> NO |
| `qps_drop` | NO | 0.00 | Required rule not matched: qps_ratio ratio_down 0.7 -> NOT m |

## 4. Task DAG
| Task ID | Type | Risk | Actions |
|---------|------|------|---------|
| `traffic_traffic_surge` | traffic_surge | medium | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `traffic_traffic_surge` | completed | 2026-06-09T08:43:22 | 2026-06-09T08:44:19 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Threads_running | {'first': 6.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 79.0, 'las | {'first': 79.0, 'las | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 349287.0,  | {'first': 452032.0,  | - |
| db_metrics.Innodb_row_lock_time | {'first': 7405874.0, | {'first': 8476769.0, | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 21.0, 'las | {'first': 18.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3002.2, 'l | {'first': 2420.2, 'l | - |
| workload.tps | {'first': 1899.6, 'l | {'first': 1554.2, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.427 |
| Performance | 0.022 |
| Target Anomaly | 0.200 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 1/5 nodes hit. Failure at: terminal_not_hit:qps_drop |

## 9. Reflection
**Failure Reason**: Propagation chain NOT fully reproduced. 1/5 nodes hit. Failure at: terminal_not_hit:qps_drop

**Suggested Changes**:
- Prior nodes need to produce more concurrent access before this node. Increase traffic_surge concurrency or ensure holder/waiter concurrency is high enough.
- The upstream nodes are not producing sufficient query pressure. Increase concurrency or duration of prior tasks.

**Parameter Updates**:
- `lock_contention`: {"holder_concurrency": "increase_by_1", "waiter_concurrency": "increase_by_4"}

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 10730
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
