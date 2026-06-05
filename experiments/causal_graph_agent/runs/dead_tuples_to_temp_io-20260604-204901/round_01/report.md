# PostgreSQL Dead Tuples Chain Report

## Metadata
- Output dir: `/Users/neo/.codex/worktrees/75bd/DB-MAGS/experiments/causal_graph_agent/runs/dead_tuples_to_temp_io-20260604-204901/round_01`
- Started at: `2026-06-04T12:49:36.817296+00:00`
- Baseline runs: `3`
- Query runs: `4`
- Inject batch size: `180000`
- Delete ratio: `0.25`
- Requested work_mem: `4MB`
- Effective work_mem: `4MB`
- Reset environment: `True`
- Force analyze after inject: `False`

## Chain Verdict
- `dead_tuples`: `hit`
- `stale_statistics`: `miss`
- `poor_plan_or_join_agg_choice`: `miss`
- `sort_or_hash_spill`: `hit`
- `repeated_or_multi_spill`: `hit`

## Baseline vs Anomaly
- Baseline median execution time: `4.57 ms`
- Anomaly max execution time: `882.28 ms`
- Join/Agg changed: `True`
- Baseline max temp bytes: `37081024`
- Anomaly max temp bytes: `67391424`

## Evidence
- Baseline filtered row count: `0`
- First anomaly filtered row count: `180001`
- Baseline n_dead_tup: `0`
- Post-injection n_dead_tup: `224991`
- Baseline last_analyze: ``
- Post-injection last_analyze: `2026-06-04 12:49:45.547398+00`
- First anomaly misestimation factor: `180001.0`
- First anomaly sort spill nodes: `2`
- First anomaly hash spill nodes: `1`
- First anomaly temp log lines: `1`

## Notes
- `plans/` contains raw `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` documents.
- `samples.jsonl` contains point-in-time catalog and temp usage snapshots.
- If `--force-analyze-after-inject` was enabled, the chain should break around stale statistics or poor plan detection.
