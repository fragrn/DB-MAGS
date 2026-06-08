"""
CLI entry point for the agent system.

Usage:
    python -m agent.cli inspect --db testdb --output snapshot.json
    python -m agent.cli plan   --request request.json --output plan.json
    python -m agent.cli run    --request request.json --output-root experiment_runs
    python -m agent.cli cleanup --run-id 20250605-123456_abc123
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from agent import __version__
from agent.config import RuntimeConfig
from agent.runtime import DBMAGSRuntime
from agent.types import ExperimentRequest


def main(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dbmags-agent",
        description="Single-agent MySQL anomaly propagation experiment system.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Probe the MySQL environment and write a snapshot.")
    p_inspect.add_argument("--db", default="testdb", help="Target database name")
    p_inspect.add_argument("--output", default="snapshot.json", help="Output file path")

    # plan
    p_plan = sub.add_parser("plan", help="Plan without executing (dry-run).")
    p_plan.add_argument("--request", required=True, help="JSON request file path")
    p_plan.add_argument("--output", default="plan.json", help="Output file path")

    # run
    p_run = sub.add_parser("run", help="Run a full anomaly propagation experiment.")
    p_run.add_argument("--request", required=True, help="JSON request file path")
    p_run.add_argument("--output-root", default="experiment_runs", help="Output root directory")
    p_run.add_argument("--no-retry", action="store_true", help="Disable automatic retry")
    p_run.add_argument("--max-rounds", type=int, default=5, help="Max retry rounds")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Cleanup a previous experiment run.")
    p_cleanup.add_argument("--run-id", required=True, help="Experiment run ID")
    p_cleanup.add_argument("--output-root", default="experiment_runs", help="Output root directory")

    parsed = parser.parse_args(args)

    config = RuntimeConfig.from_env()

    if parsed.command == "inspect":
        return cmd_inspect(config, parsed)
    elif parsed.command == "plan":
        return cmd_plan(config, parsed)
    elif parsed.command == "run":
        return cmd_run(config, parsed)
    elif parsed.command == "cleanup":
        return cmd_cleanup(config, parsed)
    return 0


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_inspect(config: RuntimeConfig, args) -> int:
    request = ExperimentRequest(target_anomaly="inspect", target_database=args.db)
    runtime = DBMAGSRuntime(config)
    snapshot = runtime.inspect(request)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False))
    print(f"Snapshot written to {output}", file=sys.stderr)
    return 0


def cmd_plan(config: RuntimeConfig, args) -> int:
    request = _load_request(args.request)
    runtime = DBMAGSRuntime(config)
    try:
        dag, snapshot = runtime.plan_only(request)
    except Exception as exc:
        print(f"Planning failed: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "tasks": {tid: t.to_dict() for tid, t in dag.tasks.items()},
        "edges": [{"source": e.source, "target": e.target} for e in dag.edges],
        "schedule": dag.schedule,
        "snapshot": snapshot.to_dict(),
    }, indent=2, ensure_ascii=False))
    print(f"Plan written to {output}", file=sys.stderr)
    return 0


def cmd_run(config: RuntimeConfig, args) -> int:
    request = _load_request(args.request)
    if args.no_retry:
        request.max_retry_rounds = 1
    elif args.max_rounds:
        request.max_retry_rounds = args.max_rounds
    runtime = DBMAGSRuntime(config)
    try:
        result = runtime.run(request, output_root=args.output_root)
    except Exception as exc:
        print(f"Experiment failed: {exc}", file=sys.stderr)
        import traceback; traceback.print_exc()
        return 1
    status = "SUCCESS" if result.evaluation.success else "FAILED"
    print(f"Run {result.run_id}: {status} | Score: {result.evaluation.final_score:.3f} | Rounds: {result.rounds}", file=sys.stderr)
    print(f"Output: {result.output_dir}", file=sys.stderr)
    return 0 if result.evaluation.success else 1


def cmd_cleanup(config: RuntimeConfig, args) -> int:
    runtime = DBMAGSRuntime(config)
    result = runtime.cleanup(args.run_id, output_root=args.output_root)
    ok = result.get("ok", False)
    if ok:
        print(f"Cleanup for {args.run_id}: OK", file=sys.stderr)
    else:
        print(f"Cleanup for {args.run_id}: FAILED", file=sys.stderr)
        for err in result.get("errors", []):
            print(f"  - {err}", file=sys.stderr)
    return 0 if ok else 1


def _load_request(path: str) -> ExperimentRequest:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Request file not found: {path}")
    data = json.loads(p.read_text())
    return ExperimentRequest.from_dict(data)


if __name__ == "__main__":
    sys.exit(main())
