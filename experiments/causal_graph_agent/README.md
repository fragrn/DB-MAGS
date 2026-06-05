# Causal Graph Agent

This experiment framework turns the anomaly propagation diagram into a constrained execution model:

- graph definition
- injector
- observer
- verifier
- scheduler
- strategy agent

The agent is intentionally constrained. It chooses chains and tuning parameters, while deterministic components execute experiments and verify evidence.

## List Chains

```bash
python3 experiments/causal_graph_agent/run_agent.py --list-chains
```

## Run the Validated PostgreSQL Chain

```bash
python3 experiments/causal_graph_agent/run_agent.py \
  --chain dead_tuples_to_temp_io \
  --max-tuning-rounds 3
```

The first implementation pass executes the previously validated PostgreSQL chain by delegating to `experiments/postgres_dead_tuples_chain/run_chain.py`.

Other chains are represented in `anomaly_graph.json` as `planned` until their concrete injectors, observers, and verifiers are implemented.
