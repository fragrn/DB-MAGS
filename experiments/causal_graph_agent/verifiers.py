from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import VerificationResult


CHAIN_STATUS_TO_NODE = {
    "dead_tuples": "dead_tuples",
    "stale_statistics": "stale_statistics",
    "poor_plan_or_join_agg_choice": "poor_plan",
    "sort_or_hash_spill": "sort_hash_spill",
    "repeated_or_multi_spill": "temp_io_workfile_write",
}


def load_pg_chain_results(output_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary_path = output_dir / "chain_summary.json"
    samples_path = output_dir / "samples.jsonl"
    if not summary_path.exists():
        raise RuntimeError(f"Missing chain summary: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    samples = []
    if samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                samples.append(json.loads(line))
    return summary, samples


def pg_summary_to_verifier_results(chain_nodes: list[str], summary: dict[str, Any]) -> list[VerificationResult]:
    chain_status = summary.get("chain_status", {})
    results: list[VerificationResult] = []
    for status_key, node_id in CHAIN_STATUS_TO_NODE.items():
        if node_id not in chain_nodes:
            continue
        hit = bool(chain_status.get(status_key, False))
        results.append(
            VerificationResult(
                node_id=node_id,
                hit=hit,
                reason=f"{status_key} {'hit' if hit else 'miss'} from PostgreSQL chain verifier",
                evidence={
                    "baseline_execution_time_ms_median": summary.get("baseline_execution_time_ms_median"),
                    "anomaly_execution_time_ms_max": summary.get("anomaly_execution_time_ms_max"),
                    "baseline_temp_bytes_max": summary.get("baseline_temp_bytes_max"),
                    "anomaly_temp_bytes_max": summary.get("anomaly_temp_bytes_max"),
                    "join_agg_changed": summary.get("join_agg_changed"),
                    "work_mem": summary.get("work_mem"),
                    "effective_work_mem": summary.get("effective_work_mem"),
                    "reset_environment": summary.get("reset_environment"),
                },
            )
        )
    return results
