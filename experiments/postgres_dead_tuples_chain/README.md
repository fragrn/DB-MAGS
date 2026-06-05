# PostgreSQL Dead Tuples Chain Experiment

This experiment is independent from the existing MySQL/TPCC workflow. It brings up a PostgreSQL environment and reproduces:

`Dead Tuples -> Stale Statistics -> Poor Plan/Join-Agg Choice -> Sort/Hash Spill`

## Quick Start

From the repository root:

```bash
python3 experiments/postgres_dead_tuples_chain/run_chain.py
```

The script will:

1. Start PostgreSQL with Docker Compose.
2. Wait for initialization to finish.
3. Capture baseline plans and statistics.
4. Inject only the cause anomaly (`Dead Tuples`) using bulk `UPDATE/DELETE`.
5. Re-run the target query, observe stale stats, plan regression, and spill evidence.
6. Write reports under `experiments/postgres_dead_tuples_chain/runs/<timestamp>/`.

## Useful Flags

```bash
python3 experiments/postgres_dead_tuples_chain/run_chain.py \
  --baseline-runs 5 \
  --inject-batch-size 120000 \
  --delete-ratio 0.25 \
  --query-runs 6 \
  --sample-interval-sec 3 \
  --shutdown
```

`--force-analyze-after-inject` is provided as a negative control. It should break the chain around the stale statistics stage.
