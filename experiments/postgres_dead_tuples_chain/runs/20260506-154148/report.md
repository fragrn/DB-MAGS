# PostgreSQL Dead Tuples Chain Report

## Metadata
- Output dir: `/Users/neo/.codex/worktrees/75bd/DB-MAGS/experiments/postgres_dead_tuples_chain/runs/20260506-154148`
- Started at: `2026-05-06T07:41:48.100571+00:00`
- Baseline runs: `3`
- Query runs: `4`
- Inject batch size: `120000`
- Delete ratio: `0.25`
- Force analyze after inject: `False`

## Chain Verdict
- `dead_tuples`: `miss`
- `stale_statistics`: `miss`
- `poor_plan_or_join_agg_choice`: `miss`
- `sort_or_hash_spill`: `hit`
- `repeated_or_multi_spill`: `hit`

## Baseline vs Anomaly
- Baseline median execution time: `394.70 ms`
- Anomaly max execution time: `397.40 ms`
- Join/Agg changed: `False`
- Baseline max temp bytes: `72519616`
- Anomaly max temp bytes: `92770240`

## Evidence
- Baseline filtered row count: `120001`
- First anomaly filtered row count: `120001`
- Baseline n_dead_tup: `149994`
- Post-injection n_dead_tup: `269994`
- Baseline last_analyze: `2026-05-06 07:38:36.179886+00`
- Post-injection last_analyze: `2026-05-06 07:38:36.179886+00`
- First anomaly misestimation factor: `120001.0`
- First anomaly sort spill nodes: `2`
- First anomaly hash spill nodes: `1`
- First anomaly temp log lines: `1`

## Notes
- `plans/` contains raw `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` documents.
- `samples.jsonl` contains point-in-time catalog and temp usage snapshots.
- If `--force-analyze-after-inject` was enabled, the chain should break around stale statistics or poor plan detection.
