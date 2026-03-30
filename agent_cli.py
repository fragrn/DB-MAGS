from __future__ import annotations

import argparse

from agent_runtime.config import RuntimeConfig
from agent_runtime.runtime import build_runtime
from agent_runtime.types import ExperimentRequest
from agent_runtime.utils import to_pretty_json


def parse_args():
    parser = argparse.ArgumentParser(description="CLI conversation runtime for DB-MAGS multi-agent anomaly planning.")
    parser.add_argument("goal", nargs="?", default="", help="User goal for the anomaly experiment.")
    parser.add_argument("--db", default="", help="Target database name.")
    parser.add_argument("--anomalies", default="", help="Comma-separated anomaly types.")
    parser.add_argument("--window", type=int, default=120, help="Execution window in seconds.")
    parser.add_argument("--risk", default="medium", help="Risk level.")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.goal:
        args.goal = input("user> Describe the anomaly experiment you want to run: ").strip()
    request = ExperimentRequest(
        user_goal=args.goal,
        target_database=args.db,
        allowed_anomalies=[item.strip() for item in args.anomalies.split(",") if item.strip()],
        execution_window_seconds=args.window,
        risk_level=args.risk,
    )
    runtime = build_runtime(RuntimeConfig.from_env())
    result = runtime.run(request)
    print("agent> Final result")
    print(to_pretty_json(result))


if __name__ == "__main__":
    main()
