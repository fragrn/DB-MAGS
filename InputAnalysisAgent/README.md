# InputAnalysisAgent

## Session-driven reproduction modes

Use Python 3.11 or newer for propagation integration. Existing runs and new
runs default to `direct`; no graph is required and the existing TaskSpec
executor is unchanged. The external Session can opt in when creating a run:

```python
runtime = CodexRuntime()
run = runtime.create(post, output_root, reproduction_mode="propagation",
                     max_reflections=1)
```

After preparation and accepted calibration, `advance()` requests `submit_graph`.
Submit a payload with `graph` (the db_repro_agent AnomalyGraph contract),
`active_injection_nodes`, and `observation_spec` (node ID to non-empty evidence
objective). TaskSpec metadata.root_cause values must exactly cover the approved
injection nodes. The graph is frozen before execution; reflection creates a new
attempt requiring a fresh graph. The configured mode is retained across attempts.

After execution, `advance()` requests `submit_propagation_evaluation`. Its payload
is `{"observations": [{"observation": {"node_id": "...", "hit": true,
"timestamp_sec": 1, "metrics": {}}, "evidence_references": ["execution_bundle.json"]}]}`.
The Session interprets actual evidence; references must point inside the current
attempt. Missing observations are listed in `missing_evidence` and prevent graph
success. All nodes (including injection nodes) and edge timing constraints must
pass before the final evaluation can claim success. Timing consistency alone is
not proof of causality. Session decisions are not internal LLM API calls.

This initial integration reuses graph validation/evaluation while retaining the
post TaskSpec execution path. It does not yet replace execution with the new
Action DAG/PropagationRunner, implement new warmup/baseline/recovery sampling,
or expose the mode on the internal-LLM `run` CLI or independent `codex-auto`.
Use the external CodexRuntime interface above, not those model-driven controllers.

`InputAnalysisAgent` converts a DBA forum post into a mechanism-level anomaly
reproduction. It separates evidence from assumptions, designs synthetic data,
calibrates with `EXPLAIN`, compiles raw TaskSpecs, executes them through the
existing DB-MAGS executor, and evaluates symptom/mechanism hits.

The legacy analysis-only command remains available:

```bash
python3 -m InputAnalysisAgent.cli --input post.txt --output plan.json
```

Run a resumable reproduction:

```bash
python3 -m InputAnalysisAgent.cli run \
  --input post.txt \
  --output-root InputAnalysisExperiment_runs \
  --interaction checkpoint
```

Exit code `2` means the run is waiting for a human decision. Review
`hitl_request.json` and the blueprint artifacts, then resume:

```bash
python3 -m InputAnalysisAgent.cli resume \
  --run-dir InputAnalysisExperiment_runs/<run_id> \
  --decision approve
```

To revise controlled fields, provide a JSON Merge Patch:

```bash
python3 -m InputAnalysisAgent.cli resume \
  --run-dir InputAnalysisExperiment_runs/<run_id> \
  --decision revise \
  --patch revision.json
```

Use `--decision feedback --feedback "..."` to ask the agent to regenerate the
blueprint from DBA guidance. Human decisions cannot modify source evidence or
bypass hard SQL/command safety checks.

Transient planner failures can be retried in the same run directory:

```bash
python3 -m InputAnalysisAgent.cli resume \
  --run-dir InputAnalysisExperiment_runs/<run_id> \
  --decision retry
```

The ReAct request timeout and attempt count are configurable through
`INPUT_ANALYSIS_LLM_TIMEOUT_SEC` (default `300`) and
`INPUT_ANALYSIS_LLM_MAX_ATTEMPTS` (default `2`).

Version 1 executes MySQL-compatible reproductions. DBMS-specific incidents for
unsupported environments are marked `blocked` and routed to Human-in-the-loop
instead of being translated into a misleading MySQL substitute.

For slow-log incidents, the runtime records a pre-execution FILE/TABLE cursor and
writes `slow_log_marker.json` plus `slow_log_evidence.json`. Success requires an
incremental fast target query entry, positive `Rows_examined`, matching EXPLAIN
calibration, and an executed plan that enables `log_queries_not_using_indexes`.
Global variables must be restored through TaskSpec `cleanup_actions`.

After data preparation, Calibration runs a dedicated ReAct loop that must call
`explain_sql` for every declared calibration query. The LLM judges the raw plan
against each query's natural-language `objective` and `expected_evidence`; the
runtime validates tool coverage and output structure but does not apply a
rule-based execution-plan matcher.
