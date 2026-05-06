#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import RuntimeConfig
from agent_runtime.planner import CATEGORY_TO_SUBTYPES
from agent_runtime.runtime import build_components
from agent_runtime.types import ExperimentRequest

CATEGORY_ORDER = ["slow_sql", "traffic_surge", "resource_bottleneck", "lock_conflict", "database_backup"]


def to_jsonable(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(val) for key, val in value.items()}
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LLM multi-agent anomaly suite against TPCC databases.")
    parser.add_argument("--db-base", default="dbmags_tpcc_base")
    parser.add_argument("--db-copy", default="dbmags_tpcc_copy")
    parser.add_argument(
        "--anomalies",
        default="all",
        help="Comma-separated anomaly subtypes or categories, or 'all'.",
    )
    parser.add_argument("--sequential", action="store_true", default=True)
    return parser.parse_args()


def expand_requested(items: str) -> List[str]:
    if items == "all":
        result: List[str] = []
        for category in CATEGORY_ORDER:
            result.extend(CATEGORY_TO_SUBTYPES[category])
        return result
    result: List[str] = []
    for raw in [item.strip() for item in items.split(",") if item.strip()]:
        if raw in CATEGORY_TO_SUBTYPES:
            result.extend(CATEGORY_TO_SUBTYPES[raw])
        else:
            result.append(raw)
    deduped: List[str] = []
    for item in result:
        if item not in deduped:
            deduped.append(item)
    return deduped


def category_for_subtype(subtype: str) -> str:
    for category, members in CATEGORY_TO_SUBTYPES.items():
        if subtype in members:
            return category
    return "slow_sql"


def database_for_subtype(subtype: str, db_base: str, db_copy: str) -> str:
    if category_for_subtype(subtype) in {"lock_conflict", "database_backup"}:
        return db_copy
    return db_base


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_root = ROOT / "experiment_runs" / "llm_multi_agent_suite" / timestamp
    output_root.mkdir(parents=True, exist_ok=True)

    config = RuntimeConfig.from_env()
    config.max_concurrency = 1
    components = build_components(config)

    requested = expand_requested(args.anomalies)
    suite_summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "db_base": args.db_base,
        "db_copy": args.db_copy,
        "openai_available": components.llm_client.available(),
        "model": config.openai_model,
        "experiments": [],
    }
    rows: List[Dict[str, object]] = []

    for subtype in requested:
        category = category_for_subtype(subtype)
        database = database_for_subtype(subtype, args.db_base, args.db_copy)
        experiment_id = f"{category}-{subtype}"
        out_dir = output_root / experiment_id
        out_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DBMAGS_MYSQL_DB"] = database
        request = ExperimentRequest(
            user_goal=f"Run an LLM-planned {subtype} anomaly experiment against TPCC data.",
            target_database=database,
            allowed_subtypes=[subtype],
            anomaly_categories=[category],
            execution_window_seconds=8,
            require_confirmation=False,
            execution_mode="sequential",
            database_topology="base_and_copy",
            user_constraints={"db_base": args.db_base, "db_copy": args.db_copy},
        )
        context = components.planner.gather_context(request)
        response = components.planner.plan(request, context)
        (out_dir / "request.json").write_text(json.dumps(to_jsonable(request), ensure_ascii=True, indent=2))
        (out_dir / "plan.json").write_text(json.dumps(to_jsonable(response), ensure_ascii=True, indent=2, default=str))

        if response.plan is None or not response.plan.tasks:
            result = {
                "experiment_id": experiment_id,
                "category": category,
                "subtype": subtype,
                "database": database,
                "status": "failed",
                "errors": response.follow_up_questions or ["planner returned no executable tasks"],
            }
            (out_dir / "task.json").write_text(json.dumps({"tasks": []}, ensure_ascii=True, indent=2))
            (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
            (out_dir / "metrics.json").write_text(json.dumps({"signals": []}, ensure_ascii=True, indent=2))
            suite_summary["experiments"].append(result)
            rows.append(result)
            continue

        (out_dir / "task.json").write_text(json.dumps(to_jsonable({"tasks": response.plan.tasks}), ensure_ascii=True, indent=2))
        task_results = components.scheduler.run(response.plan.tasks)
        status = "completed"
        if any(item.status == "failed" for item in task_results):
            status = "partial_failure"
        elif any(item.status == "executed_but_not_validated" for item in task_results):
            status = "executed_but_not_validated"
        result = {
            "experiment_id": experiment_id,
            "category": category,
            "subtype": subtype,
            "database": database,
            "status": status,
            "planner_summary": response.plan.summary,
            "task_count": len(response.plan.tasks),
            "errors": [error for item in task_results for error in item.errors],
        }
        metrics = {
            "signals": [signal for item in task_results for signal in item.observed_signals],
            "task_results": to_jsonable(task_results),
        }
        (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
        (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=True, indent=2))
        suite_summary["experiments"].append(result)
        rows.append(result)

    (output_root / "suite_summary.json").write_text(json.dumps(suite_summary, ensure_ascii=True, indent=2))
    with (output_root / "plot_ready_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["experiment_id", "category", "subtype", "database", "status", "task_count", "errors"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "experiment_id": row.get("experiment_id"),
                "category": row.get("category"),
                "subtype": row.get("subtype"),
                "database": row.get("database"),
                "status": row.get("status"),
                "task_count": row.get("task_count", 0),
                "errors": " | ".join(row.get("errors", [])) if isinstance(row.get("errors"), list) else row.get("errors", ""),
            })
    print(str(output_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
