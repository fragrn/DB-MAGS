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
`INPUT_ANALYSIS_LLM_TIMEOUT_SEC` (default `180`) and
`INPUT_ANALYSIS_LLM_MAX_ATTEMPTS` (default `2`).

Version 1 executes MySQL-compatible reproductions. DBMS-specific incidents for
unsupported environments are marked `blocked` and routed to Human-in-the-loop
instead of being translated into a misleading MySQL substitute.

For slow-log incidents, the runtime records a pre-execution FILE/TABLE cursor and
writes `slow_log_marker.json` plus `slow_log_evidence.json`. Success requires an
incremental fast target query entry, positive `Rows_examined`, matching EXPLAIN
calibration, and an executed plan that enables `log_queries_not_using_indexes`.
Global variables must be restored through TaskSpec `cleanup_actions`.
