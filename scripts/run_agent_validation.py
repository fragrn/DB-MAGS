from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import RuntimeConfig
from agent_runtime.experiment_validation import AgentValidationRunner
from agent_runtime.runtime import build_components


def parse_args():
    parser = argparse.ArgumentParser(description="Run structured validation experiments for the global agent and task agents.")
    parser.add_argument("--db", default="", help="Target database name. Defaults to DBMAGS_DEFAULT_DATABASE from env.")
    parser.add_argument("--output-root", default="", help="Optional output directory. Defaults to experiment_runs/agent_validation/<date>.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = RuntimeConfig.from_env()
    database = args.db or config.default_database
    output_root = Path(args.output_root) if args.output_root else Path("experiment_runs") / "agent_validation" / datetime.now().strftime("%Y%m%d-%H%M%S")
    runner = AgentValidationRunner(build_components(config))
    suite = runner.run(output_root=output_root, database=database)
    print(f"Validation results written to: {output_root}")
    print(f"OpenAI available: {suite['openai_available']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
