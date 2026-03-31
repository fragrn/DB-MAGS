#!/usr/bin/env python3
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import RuntimeConfig
from agent_runtime.runtime import build_components
from agent_runtime.types import ExperimentRequest


def to_jsonable(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(val) for key, val in value.items()}
    return value


EXPERIMENTS = [
    {"id": "E01_missing_index", "database": "dbmags_agent_lab", "anomaly": "missing_index", "window": 5, "constraints": {}},
    {"id": "E02_cpu", "database": "dbmags_agent_lab", "anomaly": "cpu", "window": 8, "constraints": {}},
    {
        "id": "E03_overall_workload",
        "database": "dbmags_agent_lab",
        "anomaly": "overall_workload",
        "window": 8,
        "constraints": {"baseline_sleep": 0.044, "baseline_threads": 40},
    },
    {"id": "E04_record_lock", "database": "dbmags_agent_lab_copy", "anomaly": "record_lock", "window": 5, "constraints": {}},
    {"id": "E05_table_lock", "database": "dbmags_agent_lab_copy", "anomaly": "table_lock", "window": 5, "constraints": {}},
    {"id": "E06_metadata_lock", "database": "dbmags_agent_lab_copy", "anomaly": "metadata_lock", "window": 5, "constraints": {}},
]


def main() -> int:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_root = ROOT / "experiment_runs" / "ready_experiments" / timestamp
    output_root.mkdir(parents=True, exist_ok=True)

    config = RuntimeConfig.from_env()
    config.max_concurrency = 1
    components = build_components(config)

    suite = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": config.openai_model,
        "openai_available": components.llm_client.available(),
        "experiments": [],
    }

    for spec in EXPERIMENTS:
        out_dir = output_root / spec["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DBMAGS_MYSQL_DB"] = spec["database"]

        request = ExperimentRequest(
            user_goal=f"Run agent-led anomaly experiment for {spec['anomaly']}",
            target_database=spec["database"],
            allowed_anomalies=[spec["anomaly"]],
            execution_window_seconds=spec["window"],
            require_confirmation=False,
            user_constraints=dict(spec["constraints"]),
        )
        context = components.planner.gather_context(request)
        response = components.planner.plan(request, context)

        (out_dir / "request.json").write_text(json.dumps(to_jsonable(request), ensure_ascii=True, indent=2))
        (out_dir / "plan.json").write_text(
            json.dumps(
                {
                    "context": to_jsonable(context),
                    "planner_response": to_jsonable(response),
                },
                ensure_ascii=True,
                indent=2,
                default=str,
            )
        )

        if response.follow_up_questions or response.plan is None or not response.plan.tasks:
            result = {
                "experiment_id": spec["id"],
                "agent": "GlobalPlannerAgent",
                "anomaly_type": spec["anomaly"],
                "database": spec["database"],
                "status": "failed",
                "artifacts": {"tasks": []},
                "errors": response.follow_up_questions or ["planner returned no executable tasks"],
            }
            (out_dir / "task.json").write_text(json.dumps({"tasks": []}, ensure_ascii=True, indent=2))
            (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
            suite["experiments"].append(result)
            continue

        (out_dir / "task.json").write_text(json.dumps(to_jsonable({"tasks": response.plan.tasks}), ensure_ascii=True, indent=2))
        task_results = components.scheduler.run(response.plan.tasks)
        status = "completed"
        if any(item.status == "failed" for item in task_results):
            status = "partial_failure"
        if any(item.status == "executed_but_not_validated" for item in task_results):
            status = "executed_but_not_validated"
        result = {
            "experiment_id": spec["id"],
            "agent": response.plan.tasks[0].agent_type if response.plan.tasks else "unknown",
            "anomaly_type": spec["anomaly"],
            "database": spec["database"],
            "status": status,
            "artifacts": {"task_results": to_jsonable(task_results)},
            "errors": [err for item in task_results for err in item.errors],
        }
        (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
        suite["experiments"].append(result)

    (output_root / "suite_summary.json").write_text(json.dumps(suite, ensure_ascii=True, indent=2))
    print(str(output_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
