# Causal Graph Agent Report

- Chain: `dead_tuples_to_temp_io`
- Name: `PostgreSQL optimizer spill chain`
- Complete: `False`
- Output dir: `/Users/neo/.codex/worktrees/75bd/DB-MAGS/experiments/causal_graph_agent/runs/dead_tuples_to_temp_io-20260604-204901/round_03`

## Node Verdicts
- `dead_tuples`: `hit` - dead_tuples hit from PostgreSQL chain verifier
- `stale_statistics`: `miss` - stale_statistics miss from PostgreSQL chain verifier
- `poor_plan`: `miss` - poor_plan_or_join_agg_choice miss from PostgreSQL chain verifier
- `sort_hash_spill`: `hit` - sort_or_hash_spill hit from PostgreSQL chain verifier
- `temp_io_workfile_write`: `hit` - repeated_or_multi_spill hit from PostgreSQL chain verifier

## Tuning History
- round `0` failed at `stale_statistics`; work_mem=`4MB`; reason=`stale_statistics_miss_tune_params`
- round `1` failed at `stale_statistics`; work_mem=`4MB`; reason=`stale_statistics_miss_tune_params`
- round `2` failed at `stale_statistics`; work_mem=`4MB`; reason=`no_tuning_available`
- round `3` failed at `stale_statistics`; work_mem=`4MB`; reason=`no_tuning_available`

## Raw Summary
```json
{
  "baseline_execution_time_ms_median": 4.492,
  "anomaly_execution_time_ms_max": 920.6,
  "baseline_temp_bytes_max": 37081024,
  "anomaly_temp_bytes_max": 77516736,
  "join_agg_changed": true,
  "chain_status": {
    "dead_tuples": true,
    "stale_statistics": false,
    "poor_plan_or_join_agg_choice": false,
    "sort_or_hash_spill": true,
    "repeated_or_multi_spill": true
  },
  "work_mem": "4MB",
  "effective_work_mem": "4MB",
  "reset_environment": true
}
```
