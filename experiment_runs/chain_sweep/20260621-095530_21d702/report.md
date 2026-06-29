# DBMAGS Experiment Report — 20260621-095530_21d702

**Status**: FAILED | Rounds: 2 | Final Score: 0.356

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
- **Threads**: connected=5, running=6
- **Slow Queries**: 46
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
| `long_tx_hold_tpcc_hot_rows_15s` | raw_transaction_script | Holds exclusive locks on hot TPCC rows (warehouse 1 and districts 1..10, plus a customer row) for ~12s within a 15s window. This is expected to block new-order and payment transactions, increasing lock wait time, possibly logging slow queries, and causing a brief QPS dip. Limited to 15s to satisfy safety constraints. | 1 |

## 5. Execution Trace
**Cleanup Status**: completed

| Task | Status | Start | End |
|------|--------|-------|-----|
| `long_tx_hold_tpcc_hot_rows_15s` | completed | 2026-06-21T10:09:09 | 2026-06-21T10:09:09 |

## 6. Metrics Change
| Metric | Baseline | After | Ratio |
|--------|----------|-------|-------|
| db_metrics.Threads_connected | {'first': 5.0, 'last | {'first': 5.0, 'last | - |
| db_metrics.Threads_running | {'first': 5.0, 'last | {'first': 6.0, 'last | - |
| db_metrics.Max_used_connections | {'first': 105.0, 'la | {'first': 105.0, 'la | - |
| db_metrics.Slow_queries | {'first': 46.0, 'las | {'first': 46.0, 'las | - |
| db_metrics.Innodb_row_lock_waits | {'first': 1568655.0, | {'first': 1676434.0, | - |
| db_metrics.Innodb_row_lock_time | {'first': 22430922.0 | {'first': 23536319.0 | - |
| db_metrics.Innodb_row_lock_time_avg | {'first': 14.0, 'las | {'first': 14.0, 'las | - |
| db_metrics.Aborted_connects | {'first': 0.0, 'last | {'first': 0.0, 'last | - |
| workload.qps | {'first': 3381.2, 'l | {'first': 2977.2, 'l | - |
| workload.tps | {'first': 2146.2, 'l | {'first': 1882.8, 'l | - |
| os_metrics.cpu_usage | {'usage_ratio': {'fi | {'usage_ratio': {'fi | - |

## 7. Safety
**Approved**: See below
No safety violations detected.

## 8. Evaluation Summary
| Score Component | Value |
|-----------------|-------|
| Final Score | 0.356 |
| Performance | 0.020 |
| Target Anomaly | 0.000 |
| Causal Order | 1.000 |
| Stability | 1.000 |
| Reason | Propagation chain NOT fully reproduced. 0/4 nodes hit. Failure at: terminal_not_hit:qps_drop |

## 9. Reflection
**Failure Reason**: No node fired because the long transaction did not create real contention on hot TPCC keys and/or it did not hold locks long enough while a sufficiently aligned contending workload was running. Likely issues include: autocommit left ON (no persistent locks), locking rows not touched by the running TPCC mix (misaligned predicates/warehouse), too little hold time and workload intensity (few/no waits), and the contending workload not being pinned to the same warehouses/keys. If you did not inject a TPCC workload as a contending node, the chain cannot propagate.

**Suggested Changes**:
- Make the long_tx hold row-level locks on TPCC hot keys: lock all 10 district rows for warehouses 1–3 using a single transaction (SELECT ... FOR UPDATE) and hold for ~30s before commit. This guarantees every New-Order in those warehouses conflicts.
- Ensure autocommit=0 and explicit START TRANSACTION for long_tx; use REPEATABLE-READ and FOR UPDATE so locks persist until COMMIT.
- Start long_tx first, then delay the contending workload start by ~10s so locks are already in place when requests arrive.
- Pin the lock_contention workload to the same warehouses (1–3) and drive mostly New-Order (>=90–100%) with no think time, enough terminals (≈60) and high rate to saturate those hot rows.
- Keep long_tx hold duration below lock_wait_timeout to avoid timeouts aborting claims before being logged as slow queries (e.g., hold ~30s if lock_wait_timeout is default 50s). If your lock_wait_timeout is lower, raise it temporarily (e.g., to 120s) or shorten the hold accordingly.
- Run the experiment for at least 120s so Innodb_row_lock_time and Innodb_row_lock_waits ratios clearly exceed 2.0 and slow_query_count_delta >= 1 is recorded.
- Verify alignment: EXPLAIN SELECT d_w_id,d_id FROM tpcc.district WHERE d_w_id IN (1,2,3) returns 30 rows; if not, adjust schema/table prefix or predicate.
- If your slow query threshold is high, ensure blocking > threshold (e.g., >5s). Optionally, temporarily lower long_query_time to 0.5s to guarantee slow_query_count_delta increments (requires DB config change).
- Use a single long_tx session (concurrency=1) to avoid deadlocks among lock holders while maximizing contention from many short transactions.
- If you currently have no injected TPCC workload node, add one; without concurrent conflicting operations, lock_contention, slow_query, and qps_drop cannot manifest.

**Parameter Updates**:
- `long_tx`: {"table": "tpcc.district", "predicate": "d_w_id IN (1,2,3)", "lock_mode": "FOR UPDATE", "isolation": "REPEATABLE-READ", "autocommit": false, "concurrency": 1, "duration_sec": 30, "start_delay_sec": 0, "commit_after_duration": true, "transaction_script": ["SET SESSION autocommit=0", "START TRANSACTION", "SELECT d_w_id, d_id FROM tpcc.district WHERE d_w_id IN (1,2,3) FOR UPDATE"], "on_duration_end": "COMMIT"}
- `lock_contention`: {"workload": "tpcc", "target_warehouses": [1, 2, 3], "transaction_mix": {"new_order": 1.0, "payment": 0.0, "delivery": 0.0, "order_status": 0.0, "stock_level": 0.0}, "terminals": 60, "rate": 400, "think_time_ms": 0, "start_delay_sec": 10, "duration_sec": 120}

**Risk Warning**: This will intentionally block all New-Order transactions in the targeted warehouses, causing backlog, potential lock wait timeouts, and a sharp QPS drop. Replication may lag and connection pools can saturate. Keep hold duration below lock_wait_timeout (or adjust timeout) to avoid mass timeouts. Run only in non-production, monitor closely, and ensure the long_tx always commits at the end to release locks.

## 10. Cleanup
Status: completed

## 11. Background Workload
- **Runner**: benchbase
- **Benchmark**: tpcc
- **Database**: tpcc_10W
- **PID**: 720
- **Running at End**: False
- **Exit Code**: 143

| Phase | Samples |
|-------|---------|
| baseline | 2 |
| injection | 3 |
| recovery | 1 |
