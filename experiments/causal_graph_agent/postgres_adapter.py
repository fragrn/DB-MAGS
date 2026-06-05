from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_pg_chain_module(repo_root: Path) -> ModuleType:
    script = repo_root / "experiments" / "postgres_dead_tuples_chain" / "run_chain.py"
    spec = importlib.util.spec_from_file_location("dbmags_pg_dead_tuples_run_chain", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load PostgreSQL chain module from {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_dead_tuples_chain(repo_root: Path, output_dir: Path, params: dict[str, Any], shutdown: bool = False) -> dict[str, Any]:
    module = load_pg_chain_module(repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    original_argv = list(module.sys.argv)
    work_mem_profile = params.get("work_mem_profile", "1MB")
    module.sys.argv = [
        "run_chain.py",
        "--baseline-runs",
        str(params.get("baseline_runs", 3)),
        "--inject-batch-size",
        str(params.get("inject_batch_size", 120000)),
        "--delete-ratio",
        str(params.get("delete_ratio", 0.25)),
        "--query-runs",
        str(params.get("query_runs", 4)),
        "--sample-interval-sec",
        str(params.get("sample_interval_sec", 2)),
        "--output-dir",
        str(output_dir),
        "--work-mem",
        str(work_mem_profile),
    ]
    if params.get("reset_environment", True):
        module.sys.argv.append("--reset-environment")
    if params.get("force_analyze_after_inject"):
        module.sys.argv.append("--force-analyze-after-inject")
    if shutdown:
        module.sys.argv.append("--shutdown")
    try:
        exit_code = module.main()
    finally:
        module.sys.argv = original_argv
    return {"exit_code": exit_code, "output_dir": str(output_dir)}
