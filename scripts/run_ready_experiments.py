#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import RuntimeConfig
from agent_runtime.runtime import build_components
from agent_runtime.types import ExperimentRequest


CATEGORY_ORDER = [
    "slow_sql",
    "traffic_surge",
    "resource_bottleneck",
    "lock_conflict",
    "database_backup",
]

EXPERIMENTS = [
    {"id": "E01_missing_index", "category": "slow_sql", "subtype": "missing_index", "window": 5},
    {"id": "E02_excessive_index", "category": "slow_sql", "subtype": "excessive_index", "window": 5},
    {"id": "E03_implicit_conversion", "category": "slow_sql", "subtype": "implicit_conversion", "window": 5},
    {"id": "E04_multi_table_join", "category": "slow_sql", "subtype": "multi_table_join", "window": 5},
    {"id": "E05_order_by", "category": "slow_sql", "subtype": "order_by", "window": 5},
    {"id": "E06_group_by", "category": "slow_sql", "subtype": "group_by", "window": 5},
    {"id": "E07_large_table_scan", "category": "slow_sql", "subtype": "large_table_scan", "window": 5},
    {
        "id": "E08_single_sql",
        "category": "traffic_surge",
        "subtype": "single_sql",
        "window": 8,
        "constraints": {"baseline_sleep": 0.02, "baseline_threads": 12},
    },
    {
        "id": "E09_overall_workload",
        "category": "traffic_surge",
        "subtype": "overall_workload",
        "window": 8,
        "constraints": {"baseline_sleep": 0.044, "baseline_threads": 40},
    },
    {"id": "E10_cpu", "category": "resource_bottleneck", "subtype": "cpu", "window": 8},
    {"id": "E11_io", "category": "resource_bottleneck", "subtype": "io", "window": 8},
    {"id": "E12_network", "category": "resource_bottleneck", "subtype": "network", "window": 8},
    {"id": "E13_memory", "category": "resource_bottleneck", "subtype": "memory", "window": 8},
    {"id": "E14_disk", "category": "resource_bottleneck", "subtype": "disk", "window": 8},
    {"id": "E15_record_lock", "category": "lock_conflict", "subtype": "record_lock", "window": 5},
    {"id": "E16_table_lock", "category": "lock_conflict", "subtype": "table_lock", "window": 5},
    {"id": "E17_metadata_lock", "category": "lock_conflict", "subtype": "metadata_lock", "window": 5},
    {"id": "E18_database_table_backup", "category": "database_backup", "subtype": "database_table_backup", "window": 5},
]


def parse_args():
    parser = argparse.ArgumentParser(description="Run the full LLM-driven anomaly suite against the TPCC base/copy databases.")
    parser.add_argument("--db-base", default="dbmags_tpcc_base")
    parser.add_argument("--db-copy", default="dbmags_tpcc_copy")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--anomalies", default="", help="Optional comma-separated subtype filter.")
    parser.add_argument("--sequential", action="store_true", help="Keep execution sequential. Enabled by default.")
    return parser.parse_args()


def to_jsonable(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(val) for key, val in value.items()}
    return value


def select_experiments(subtypes: set[str]) -> list[dict[str, Any]]:
    if not subtypes:
        return list(EXPERIMENTS)
    return [item for item in EXPERIMENTS if item["subtype"] in subtypes]


def database_for_spec(spec: dict[str, Any], db_base: str, db_copy: str) -> str:
    return db_copy if spec["category"] in {"lock_conflict", "database_backup"} else db_base


def experiment_request(spec: dict[str, Any], database: str) -> ExperimentRequest:
    return ExperimentRequest(
        user_goal=f"Run LLM-driven anomaly experiment for {spec['subtype']}",
        target_database=database,
        allowed_subtypes=[spec["subtype"]],
        anomaly_categories=[spec["category"]],
        execution_window_seconds=spec["window"],
        require_confirmation=False,
        execution_mode="sequential",
        database_topology="base_and_copy",
        user_constraints=dict(spec.get("constraints", {})),
    )


def result_status(task_results: list[Any]) -> str:
    status = "completed"
    if any(item.status == "failed" for item in task_results):
        status = "partial_failure"
    if any(item.status == "executed_but_not_validated" for item in task_results):
        status = "executed_but_not_validated"
    return status


def flatten_errors(task_results: list[Any]) -> list[str]:
    return [error for item in task_results for error in item.errors]


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        path.write_text("")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output_root) if args.output_root else ROOT / "experiment_runs" / "ready_experiments" / timestamp
    output_root.mkdir(parents=True, exist_ok=True)

    config = RuntimeConfig.from_env()
    config.max_concurrency = 1
    config.default_database = args.db_base
    components = build_components(config)

    requested_subtypes = {item.strip() for item in args.anomalies.split(",") if item.strip()}
    experiments = select_experiments(requested_subtypes)

    suite = {
        "generated_at": datetime.now(UTC).isoformat(),
        "db_base": args.db_base,
        "db_copy": args.db_copy,
        "model": config.openai_model,
        "openai_available": components.llm_client.available(),
        "execution_mode": "sequential",
        "experiments": [],
    }
    summary_rows = []

    for spec in experiments:
        out_dir = output_root / spec["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        database = database_for_spec(spec, args.db_base, args.db_copy)
        request = experiment_request(spec, database)

        context = components.planner.gather_context(request)
        response = components.planner.plan(request, context)

        (out_dir / "request.json").write_text(json.dumps(to_jsonable(request), ensure_ascii=True, indent=2))
        (out_dir / "plan.json").write_text(
            json.dumps({"context": to_jsonable(context), "planner_response": to_jsonable(response)}, ensure_ascii=True, indent=2)
        )

        if response.follow_up_questions or response.plan is None or not response.plan.tasks:
            result = {
                "experiment_id": spec["id"],
                "category": spec["category"],
                "subtype": spec["subtype"],
                "database": database,
                "status": "failed",
                "agent": "GlobalPlannerAgent",
                "artifacts": {"tasks": []},
                "errors": response.follow_up_questions or ["planner returned no executable tasks"],
            }
            (out_dir / "task.json").write_text(json.dumps({"tasks": []}, ensure_ascii=True, indent=2))
            (out_dir / "metrics.json").write_text(json.dumps({"observed_signals": [], "task_count": 0}, ensure_ascii=True, indent=2))
            (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
            suite["experiments"].append(result)
            summary_rows.append(
                {
                    "experiment_id": spec["id"],
                    "category": spec["category"],
                    "subtype": spec["subtype"],
                    "database": database,
                    "status": "failed",
                    "task_count": 0,
                    "error_count": len(result["errors"]),
                }
            )
            continue

        (out_dir / "task.json").write_text(json.dumps(to_jsonable({"tasks": response.plan.tasks}), ensure_ascii=True, indent=2))
        task_results = components.scheduler.run(response.plan.tasks)
        metrics = {
            "observed_signals": [signal for item in task_results for signal in item.observed_signals],
            "task_count": len(task_results),
            "cleanup_statuses": {item.task_id: item.cleanup_status for item in task_results},
        }
        status = result_status(task_results)
        result = {
            "experiment_id": spec["id"],
            "category": spec["category"],
            "subtype": spec["subtype"],
            "database": database,
            "status": status,
            "agent": response.plan.tasks[0].agent_type if response.plan.tasks else "unknown",
            "artifacts": {"task_results": to_jsonable(task_results)},
            "errors": flatten_errors(task_results),
        }
        (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=True, indent=2))
        (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
        suite["experiments"].append(result)
        summary_rows.append(
            {
                "experiment_id": spec["id"],
                "category": spec["category"],
                "subtype": spec["subtype"],
                "database": database,
                "status": status,
                "task_count": len(task_results),
                "error_count": len(result["errors"]),
                "observed_signals": "; ".join(metrics["observed_signals"]),
            }
        )

    (output_root / "suite_summary.json").write_text(json.dumps(suite, ensure_ascii=True, indent=2))
    write_csv(output_root / "plot_ready_summary.csv", summary_rows)
    print(str(output_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
