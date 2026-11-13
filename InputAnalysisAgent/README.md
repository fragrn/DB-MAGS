# InputAnalysisAgent

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
## Evidence-grounded evaluation (v2)

Opt in with `run --evaluation-version 2`, or submit a Blueprint with
`evaluation_spec.version: 2` through `CodexRuntime`. Existing Blueprints stay
on the legacy evaluator.

Each required dimension (`symptom`, `mechanism`, `environment`) needs at least
one criterion. Criteria declare an ID, kind, description, basis in the post,
and named JSON pointers into the captured execution bundle. Numeric checks
compare a value, ratio, or delta with a predeclared threshold; event checks
compare an observed scalar; semantic checks require current-attempt evidence IDs.
For example, one numeric criterion can be:

```json
{
  "id": "query_latency", "dimension": "symptom", "kind": "numeric",
  "required": true, "description": "Target workload is slower than the control",
  "post_basis": "The post reports higher latency for the target query",
  "inputs": {
    "target": "/execution_actions/comparison/1/latency_ms/p95",
    "control": "/execution_actions/comparison/0/latency_ms/p95"
  },
  "operation": "ratio", "operator": ">=", "threshold": 2,
  "threshold_source": "Experiment criterion, not a value stated in the post"
}
```

`CodexRuntime.evaluation_evidence(run_dir)` returns the reference IDs for
`submit_evaluation(run_dir, {"judgments": {...}})`. Each judgment has
`status`, `reason`, and `evidence_ids`; the driver does not submit the verdict.
The runtime freezes criteria before execution, seals the captured evidence,
and stores both alongside the verdict. The evaluator requires all required
criteria to hit for success. Missing observations remain unknown. No similarity
score is inferred, and ordinary EXPLAIN is not a measurement of actual rows.
