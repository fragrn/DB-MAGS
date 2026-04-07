import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from agent.config import RuntimeConfig
from agent.executor import TaskExecutor
from agent.global_agent import GlobalAgent
from agent.llm import ResponsesAPIClient


def parse_args():
    parser = argparse.ArgumentParser(description="Run rule-based DB-MAGS agent orchestration.")
    parser.add_argument("--schema", default=None, help="Schema to inspect. Defaults to the configured DB name.")
    parser.add_argument("--duration", type=int, default=120, help="Total experiment duration in seconds.")
    parser.add_argument("--fault-inject-time", type=int, default=60, help="Seconds to wait before each task starts.")
    parser.add_argument("--fault-duration", type=int, default=60, help="Duration for each injected anomaly task.")
    parser.add_argument(
        "--agents",
        default="cpu_contention,missing_index",
        help="Comma-separated task agents to enable.",
    )
    parser.add_argument("--output-dir", default=None, help="Directory for plan, results, and task logs.")
    parser.add_argument("--query-repeat", type=int, default=10, help="How many times to inject the missing-index query.")
    parser.add_argument("--query-sleep", type=float, default=5.0, help="Sleep between repeated query injections.")
    parser.add_argument("--min-row-count", type=int, default=1000, help="Minimum table size for missing-index planning.")
    parser.add_argument("--cpu-load", type=int, default=95, help="Target CPU load percent for ChaosBlade.")
    parser.add_argument("--cpu-core-count", type=int, default=1, help="How many CPU workers ChaosBlade should load.")
    parser.add_argument("--chaosblade-path", default=None, help="Path to the ChaosBlade blade binary.")
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = args.output_dir or str(Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    runtime_context = {
        "enabled_agents": [item.strip() for item in args.agents.split(",") if item.strip()],
        "fault_inject_time": args.fault_inject_time,
        "fault_duration": args.fault_duration,
        "query_repeat": args.query_repeat,
        "query_sleep": args.query_sleep,
        "min_row_count": args.min_row_count,
        "cpu_load": args.cpu_load,
        "cpu_core_count": args.cpu_core_count,
        "chaosblade_path": args.chaosblade_path,
        "duration": args.duration,
    }

    config = RuntimeConfig.from_env(base_dir=Path(__file__).resolve().parent)
    llm_client = ResponsesAPIClient(config)
    global_agent = GlobalAgent(llm_client=llm_client)
    profile = global_agent.collect_profile(schema_name=args.schema)
    plan = global_agent.plan(profile, runtime_context)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    with (output_path / "database_profile.json").open("w", encoding="utf-8") as handle:
        json.dump(asdict(profile), handle, indent=2, default=str)
    with (output_path / "runtime.json").open("w", encoding="utf-8") as handle:
        json.dump(global_agent.runtime_metadata(), handle, indent=2, default=str)

    executor = TaskExecutor(output_dir=output_dir, runtime_metadata=global_agent.runtime_metadata())
    report = executor.execute_plan(plan)
    print(json.dumps(report.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
