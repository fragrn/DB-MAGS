from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
AGENT_RUN = ROOT / "agent_run.py"
DEFAULT_RUNS = [
    {
        "run_name": "missing_r1",
        "scenario": "missing_only",
        "agents": "missing_index",
        "query_repeat": 1,
        "cpu_load": None,
        "cpu_core_count": None,
        "fault_duration": 1,
    },
    {
        "run_name": "missing_r5",
        "scenario": "missing_only",
        "agents": "missing_index",
        "query_repeat": 5,
        "cpu_load": None,
        "cpu_core_count": None,
        "fault_duration": 1,
    },
    {
        "run_name": "missing_r20",
        "scenario": "missing_only",
        "agents": "missing_index",
        "query_repeat": 20,
        "cpu_load": None,
        "cpu_core_count": None,
        "fault_duration": 1,
    },
    {
        "run_name": "cpu_l30",
        "scenario": "cpu_only",
        "agents": "cpu_contention",
        "query_repeat": None,
        "cpu_load": 30,
        "cpu_core_count": 1,
        "fault_duration": 1,
    },
    {
        "run_name": "cpu_l60",
        "scenario": "cpu_only",
        "agents": "cpu_contention",
        "query_repeat": None,
        "cpu_load": 60,
        "cpu_core_count": 1,
        "fault_duration": 1,
    },
    {
        "run_name": "cpu_l95",
        "scenario": "cpu_only",
        "agents": "cpu_contention",
        "query_repeat": None,
        "cpu_load": 95,
        "cpu_core_count": 1,
        "fault_duration": 1,
    },
    {
        "run_name": "combined_l95_r20",
        "scenario": "combined",
        "agents": "cpu_contention,missing_index",
        "query_repeat": 20,
        "cpu_load": 95,
        "cpu_core_count": 1,
        "fault_duration": 1,
    },
]
DETAIL_FIELDS = [
    "run_name",
    "scenario",
    "agents",
    "task_type",
    "agent_name",
    "status",
    "elapsed_seconds",
    "query_repeat",
    "cpu_load",
    "cpu_core_count",
    "fault_duration",
    "cleanup_failed",
    "table",
    "column",
    "sql",
    "chaosblade_uid",
    "openai_available",
    "openai_connected",
    "openai_model",
    "openai_endpoint",
    "openai_error",
    "planner_summary",
    "log",
    "output_dir",
]
RUN_FIELDS = [
    "run_name",
    "scenario",
    "agents",
    "task_count",
    "successful_task_count",
    "cleanup_failed_task_count",
    "run_wall_time_seconds",
    "max_task_elapsed_seconds",
    "query_repeat",
    "cpu_load",
    "cpu_core_count",
    "fault_duration",
    "openai_available",
    "openai_connected",
    "openai_model",
    "openai_endpoint",
    "openai_error",
    "output_dir",
]


@dataclass
class RunArtifacts:
    config: Dict[str, Any]
    output_dir: Path
    results: Dict[str, Any]


def _iso_elapsed(result: Dict[str, Any]) -> float:
    started = result.get("started_at")
    finished = result.get("finished_at")
    if not started or not finished:
        return 0.0
    start_dt = datetime.fromisoformat(started)
    finish_dt = datetime.fromisoformat(finished)
    return max(0.0, (finish_dt - start_dt).total_seconds())


def _run_one(base_dir: Path, schema: str, config: Dict[str, Any]) -> RunArtifacts:
    output_dir = base_dir / config["run_name"]
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(AGENT_RUN),
        "--schema",
        schema,
        "--agents",
        config["agents"],
        "--fault-inject-time",
        "0",
        "--fault-duration",
        str(config["fault_duration"]),
        "--output-dir",
        str(output_dir),
    ]
    if config.get("query_repeat") is not None:
        cmd.extend(["--query-repeat", str(config["query_repeat"]), "--query-sleep", "0"])
    if config.get("cpu_load") is not None:
        cmd.extend(["--cpu-load", str(config["cpu_load"])])
    if config.get("cpu_core_count") is not None:
        cmd.extend(["--cpu-core-count", str(config["cpu_core_count"])])

    subprocess.run(cmd, cwd=str(ROOT), check=True)
    results = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    return RunArtifacts(config=config, output_dir=output_dir, results=results)


def _detail_rows(artifacts: List[RunArtifacts]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for artifact in artifacts:
        base = artifact.results
        for result in base.get("results", []):
            elapsed = _iso_elapsed(result)
            rows.append(
                {
                    "run_name": artifact.config["run_name"],
                    "scenario": artifact.config["scenario"],
                    "agents": artifact.config["agents"],
                    "task_type": result.get("task_type", ""),
                    "agent_name": result.get("agent_name", ""),
                    "status": result.get("status", ""),
                    "elapsed_seconds": f"{elapsed:.6f}",
                    "query_repeat": artifact.config.get("query_repeat") or "",
                    "cpu_load": artifact.config.get("cpu_load") or "",
                    "cpu_core_count": artifact.config.get("cpu_core_count") or "",
                    "fault_duration": artifact.config.get("fault_duration", ""),
                    "cleanup_failed": "false",
                    "table": result.get("metadata", {}).get("table", ""),
                    "column": result.get("metadata", {}).get("column", ""),
                    "sql": result.get("artifacts", {}).get("sql", ""),
                    "chaosblade_uid": result.get("artifacts", {}).get("chaosblade_uid", ""),
                    "openai_available": str(bool(base.get("openai_available"))).lower(),
                    "openai_connected": str(bool(base.get("openai_connected"))).lower(),
                    "openai_model": base.get("openai_model", "") or "",
                    "openai_endpoint": base.get("openai_endpoint", "") or "",
                    "openai_error": base.get("openai_error", "") or "",
                    "planner_summary": base.get("planner_summary", "") or "",
                    "log": result.get("artifacts", {}).get("log", ""),
                    "output_dir": str(artifact.output_dir.relative_to(ROOT)),
                }
            )
    return rows


def _run_rows(artifacts: List[RunArtifacts], detail_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in detail_rows:
        grouped.setdefault(row["run_name"], []).append(row)
    rows: List[Dict[str, Any]] = []
    for artifact in artifacts:
        run_name = artifact.config["run_name"]
        items = grouped[run_name]
        elapsed = [float(item["elapsed_seconds"]) for item in items]
        rows.append(
            {
                "run_name": run_name,
                "scenario": artifact.config["scenario"],
                "agents": artifact.config["agents"],
                "task_count": len(items),
                "successful_task_count": sum(1 for item in items if item["status"] == "success"),
                "cleanup_failed_task_count": 0,
                "run_wall_time_seconds": f"{max(elapsed, default=0.0):.6f}",
                "max_task_elapsed_seconds": f"{max(elapsed, default=0.0):.6f}",
                "query_repeat": artifact.config.get("query_repeat") or "",
                "cpu_load": artifact.config.get("cpu_load") or "",
                "cpu_core_count": artifact.config.get("cpu_core_count") or "",
                "fault_duration": artifact.config.get("fault_duration", ""),
                "openai_available": str(bool(artifact.results.get("openai_available"))).lower(),
                "openai_connected": str(bool(artifact.results.get("openai_connected"))).lower(),
                "openai_model": artifact.results.get("openai_model", "") or "",
                "openai_endpoint": artifact.results.get("openai_endpoint", "") or "",
                "openai_error": artifact.results.get("openai_error", "") or "",
                "output_dir": str(artifact.output_dir.relative_to(ROOT)),
            }
        )
    return rows


def _write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    schema = os.getenv("DBMAGS_MYSQL_DB", "dbmags_agent_lab")
    matrix_dir = ROOT / "experiment_runs" / f"{datetime.now().strftime('%Y%m%d')}-vectorengine-matrix"
    matrix_dir.mkdir(parents=True, exist_ok=True)

    artifacts = [_run_one(matrix_dir, schema, config) for config in DEFAULT_RUNS]
    detail_rows = _detail_rows(artifacts)
    run_rows = _run_rows(artifacts, detail_rows)

    (matrix_dir / "plot_ready_summary.json").write_text(json.dumps(detail_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    (matrix_dir / "plot_ready_run_summary.json").write_text(json.dumps(run_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(matrix_dir / "plot_ready_summary.csv", DETAIL_FIELDS, detail_rows)
    _write_csv(matrix_dir / "plot_ready_run_summary.csv", RUN_FIELDS, run_rows)
    print(json.dumps({"matrix_dir": str(matrix_dir), "run_count": len(run_rows)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
