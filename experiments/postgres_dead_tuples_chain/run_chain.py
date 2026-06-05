#!/usr/bin/env python3
"""Drive a PostgreSQL experiment for:

Dead Tuples -> Stale Statistics -> Poor Plan/Join-Agg Choice -> Sort/Hash Spill
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = SCRIPT_DIR / "docker-compose.yml"
MAIN_QUERY_FILE = SCRIPT_DIR / "main_query.sql"
SERVICE_NAME = "postgres"
DATABASE = "dbmags_pg_chain"
USER = "postgres"
PASSWORD = "postgres"
DEFAULT_OUTPUT_ROOT = SCRIPT_DIR / "runs"
ALLOWED_WORK_MEM_VALUES = ("512kB", "1MB", "2MB", "4MB")
DEFAULT_WORK_MEM = "1MB"
COMPOSE_WORK_MEM = os.environ.get("DBMAGS_PG_WORK_MEM", DEFAULT_WORK_MEM)

TARGET_QUERY_TEMPLATE = MAIN_QUERY_FILE.read_text(encoding="utf-8").strip()


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        message = [
            f"Command failed: {' '.join(cmd)}",
            f"exit_code={exc.returncode}",
        ]
        if exc.stdout:
            message.append(f"stdout:\n{exc.stdout.strip()}")
        if exc.stderr:
            message.append(f"stderr:\n{exc.stderr.strip()}")
        raise RuntimeError("\n".join(message)) from exc


def compose_cmd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    env = os.environ.copy()
    env["DBMAGS_PG_WORK_MEM"] = COMPOSE_WORK_MEM
    return run_cmd(cmd, cwd=SCRIPT_DIR, check=check, env=env)


def compose_exec(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return compose_cmd("exec", "-T", SERVICE_NAME, *args, check=check)


def run_psql(sql: str, *, tuples_only: bool = True, no_align: bool = True) -> str:
    cmd = [
        "env",
        f"PGPASSWORD={PASSWORD}",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        USER,
        "-d",
        DATABASE,
    ]
    if tuples_only:
        cmd.append("-t")
    if no_align:
        cmd.append("-A")
    cmd.extend(["-c", sql])
    result = compose_exec(*cmd)
    return result.stdout.strip()


def run_explain_json(query: str) -> dict[str, Any]:
    sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}"
    out = run_psql(sql)
    data = json.loads(out)
    return data[0]


def get_effective_work_mem() -> str:
    return run_psql("SHOW work_mem;")


def load_query() -> str:
    return TARGET_QUERY_TEMPLATE


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def wait_for_postgres(timeout_sec: int = 180) -> None:
    start = time.time()
    while time.time() - start < timeout_sec:
        result = compose_exec(
            "env",
            f"PGPASSWORD={PASSWORD}",
            "pg_isready",
            "-U",
            USER,
            "-d",
            DATABASE,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise TimeoutError("PostgreSQL did not become ready in time.")


def ensure_environment_up(reset_environment: bool = False) -> None:
    if reset_environment:
        compose_cmd("down", "--volumes", check=False)
    compose_cmd("up", "-d")
    wait_for_postgres()


def maybe_shutdown(shutdown: bool) -> None:
    if shutdown:
        try:
            compose_cmd("down", "--volumes")
        except RuntimeError as exc:
            print(f"warning: docker compose down failed: {exc}", file=sys.stderr)


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def fetch_single_json(sql: str) -> dict[str, Any]:
    out = run_psql(sql)
    if not out:
        return {}
    return json.loads(out)


def get_main_query_actual_count() -> int:
    sql = """
    SELECT count(*)
    FROM fact_events
    WHERE tenant_id = 42
      AND score >= 900
    """
    return int(run_psql(sql))


def sample_state() -> dict[str, Any]:
    exact_count = get_main_query_actual_count()
    sql = f"""
    WITH table_stats AS (
        SELECT
            relid::regclass::text AS relname,
            n_live_tup,
            n_dead_tup,
            seq_scan,
            idx_scan,
            last_analyze,
            last_autoanalyze
        FROM pg_stat_user_tables
        WHERE relname = 'fact_events'
    ),
    rel AS (
        SELECT reltuples::bigint AS reltuples
        FROM pg_class
        WHERE relname = 'fact_events'
    ),
    db AS (
        SELECT temp_files, temp_bytes
        FROM pg_stat_database
        WHERE datname = current_database()
    )
    SELECT json_build_object(
        'captured_at', {sql_literal(now_utc())},
        'exact_filtered_row_count', {exact_count},
        'n_live_tup', (SELECT n_live_tup FROM table_stats),
        'n_dead_tup', (SELECT n_dead_tup FROM table_stats),
        'seq_scan', (SELECT seq_scan FROM table_stats),
        'idx_scan', (SELECT idx_scan FROM table_stats),
        'last_analyze', COALESCE((SELECT last_analyze::text FROM table_stats), ''),
        'last_autoanalyze', COALESCE((SELECT last_autoanalyze::text FROM table_stats), ''),
        'stats_reltuples', (SELECT reltuples FROM rel),
        'table_bytes', pg_total_relation_size('fact_events'),
        'temp_files', (SELECT temp_files FROM db),
        'temp_bytes', (SELECT temp_bytes FROM db),
        'pg_stat_statements_rows',
            (SELECT COALESCE(sum(rows), 0)
             FROM pg_stat_statements
             WHERE query LIKE 'SELECT%fact_events%tenant_id = 42%score >= 900%')
    )
    """
    return fetch_single_json(sql)


def inject_dead_tuples(batch_size: int, delete_ratio: float) -> dict[str, Any]:
    delete_limit = int(batch_size * delete_ratio)
    sql = f"""
    BEGIN;
    UPDATE fact_events
    SET tenant_id = 42,
        score = 980,
        category_id = ((category_id + 17) % 200) + 1,
        amount = amount + 250,
        payload = payload || repeat('x', 32),
        updated_at = now()
    WHERE event_id <= {batch_size};

    DELETE FROM fact_events
    WHERE event_id > 500000 - {delete_limit}
      AND tenant_id <> 42;
    COMMIT;
    """
    run_psql(sql, tuples_only=False, no_align=False)
    sample = sample_state()
    sample["delete_limit"] = delete_limit
    sample["updated_rows"] = batch_size
    return sample


def maybe_force_analyze(force_analyze: bool) -> None:
    if force_analyze:
        run_psql("ANALYZE fact_events;", tuples_only=False, no_align=False)


def flatten_plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan["Plan"])
    return nodes


def extract_plan_summary(explain_doc: dict[str, Any]) -> dict[str, Any]:
    nodes = flatten_plan_nodes(explain_doc)
    node_types = [node.get("Node Type", "") for node in nodes]
    join_nodes = [n for n in node_types if "Join" in n]
    agg_nodes = [n for n in node_types if "Aggregate" in n]
    sort_spill_nodes = []
    hash_spill_nodes = []
    max_misestimation_factor = 1.0
    max_misestimation_node = None
    fact_scan = None

    for node in nodes:
        est_rows = float(node.get("Plan Rows", 0) or 0)
        act_rows = float((node.get("Actual Rows", 0) or 0) * (node.get("Actual Loops", 1) or 1))
        if est_rows > 0 and act_rows > 0:
            factor = max(act_rows / est_rows, est_rows / act_rows)
            if factor > max_misestimation_factor:
                max_misestimation_factor = factor
                max_misestimation_node = node.get("Node Type", "")

        if node.get("Relation Name") == "fact_events" and fact_scan is None:
            fact_scan = {
                "node_type": node.get("Node Type"),
                "plan_rows": node.get("Plan Rows"),
                "actual_rows": node.get("Actual Rows"),
                "actual_loops": node.get("Actual Loops"),
            }

        sort_method = str(node.get("Sort Method", "")).lower()
        sort_space_type = str(node.get("Sort Space Type", "")).lower()
        temp_written = int(node.get("Temp Written Blocks", 0) or 0)
        disk_usage = int(node.get("Disk Usage", 0) or 0)
        hashagg_batches = int(node.get("HashAgg Batches", 1) or 1)
        hash_batches = int(node.get("Hash Batches", 1) or 1)

        if node.get("Node Type") == "Sort" and ("external" in sort_method or sort_space_type == "disk" or temp_written > 0):
            sort_spill_nodes.append(
                {
                    "node_type": node.get("Node Type"),
                    "sort_method": node.get("Sort Method"),
                    "sort_space_type": node.get("Sort Space Type"),
                    "sort_space_used": node.get("Sort Space Used"),
                    "temp_written_blocks": temp_written,
                }
            )

        if (
            node.get("Node Type") in {"Hash", "HashAggregate", "Aggregate"}
            and (disk_usage > 0 or hashagg_batches > 1 or hash_batches > 1 or temp_written > 0)
        ):
            hash_spill_nodes.append(
                {
                    "node_type": node.get("Node Type"),
                    "disk_usage": disk_usage,
                    "hashagg_batches": hashagg_batches,
                    "hash_batches": hash_batches,
                    "temp_written_blocks": temp_written,
                }
            )

    return {
        "planning_time_ms": explain_doc.get("Planning Time"),
        "execution_time_ms": explain_doc.get("Execution Time"),
        "node_types": node_types,
        "join_nodes": join_nodes,
        "agg_nodes": agg_nodes,
        "fact_scan": fact_scan,
        "sort_spill_nodes": sort_spill_nodes,
        "hash_spill_nodes": hash_spill_nodes,
        "max_misestimation_factor": round(max_misestimation_factor, 3),
        "max_misestimation_node": max_misestimation_node,
    }


def collect_logs_since(since_utc: str) -> str:
    result = compose_cmd("logs", "--no-color", "--since", since_utc, SERVICE_NAME)
    return result.stdout


def temp_log_lines(log_text: str) -> list[str]:
    return [line for line in log_text.splitlines() if "temporary file" in line.lower()]


@dataclass
class PlanRun:
    phase: str
    run_index: int
    explain_doc: dict[str, Any]
    summary: dict[str, Any]
    sample: dict[str, Any]
    temp_log_lines: list[str]


def execute_query_and_collect(phase: str, run_index: int, query: str, log_since: str) -> PlanRun:
    explain_doc = run_explain_json(query)
    summary = extract_plan_summary(explain_doc)
    sample = sample_state()
    logs = collect_logs_since(log_since)
    return PlanRun(
        phase=phase,
        run_index=run_index,
        explain_doc=explain_doc,
        summary=summary,
        sample=sample,
        temp_log_lines=temp_log_lines(logs),
    )


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, payload: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def summarize_chain(
    baseline_runs: list[PlanRun],
    anomaly_runs: list[PlanRun],
    injection_sample: dict[str, Any],
) -> dict[str, Any]:
    baseline_exec = [run.summary["execution_time_ms"] for run in baseline_runs]
    anomaly_exec = [run.summary["execution_time_ms"] for run in anomaly_runs]
    baseline_temp_bytes = [run.sample["temp_bytes"] for run in baseline_runs]
    anomaly_temp_bytes = [run.sample["temp_bytes"] for run in anomaly_runs]
    baseline_median = statistics.median(baseline_exec) if baseline_exec else 0.0
    anomaly_max = max(anomaly_exec) if anomaly_exec else 0.0

    first_baseline = baseline_runs[0]
    first_anomaly = anomaly_runs[0]

    dead_tuples = injection_sample.get("n_dead_tup", 0) > max(first_baseline.sample.get("n_dead_tup", 0) * 5, 1000)
    stale_stats = (
        injection_sample.get("last_analyze") == first_baseline.sample.get("last_analyze")
        and first_anomaly.summary["max_misestimation_factor"] >= 5.0
        and first_anomaly.sample.get("exact_filtered_row_count", 0) > max(first_baseline.sample.get("exact_filtered_row_count", 0) * 10, 1000)
    )

    join_agg_changed = (
        first_baseline.summary["join_nodes"] != first_anomaly.summary["join_nodes"]
        or first_baseline.summary["agg_nodes"] != first_anomaly.summary["agg_nodes"]
    )
    poor_plan = (
        stale_stats
        and anomaly_max >= max(2.0 * baseline_median, baseline_median + 50)
        and (
            join_agg_changed
            or first_anomaly.summary["max_misestimation_factor"] >= 10.0
            or bool(first_anomaly.summary["hash_spill_nodes"])
        )
    )

    spill_run_count = sum(
        1
        for run in anomaly_runs
        if run.summary["sort_spill_nodes"] or run.summary["hash_spill_nodes"] or run.temp_log_lines
    )
    spill = spill_run_count >= 1 and any(
        run.summary["sort_spill_nodes"] or run.summary["hash_spill_nodes"]
        for run in anomaly_runs
    )
    repeated_spill = spill_run_count >= 2 or any(
        len(run.summary["sort_spill_nodes"]) + len(run.summary["hash_spill_nodes"]) >= 2
        for run in anomaly_runs
    )

    return {
        "baseline_execution_time_ms_median": baseline_median,
        "anomaly_execution_time_ms_max": anomaly_max,
        "baseline_temp_bytes_max": max(baseline_temp_bytes) if baseline_temp_bytes else 0,
        "anomaly_temp_bytes_max": max(anomaly_temp_bytes) if anomaly_temp_bytes else 0,
        "join_agg_changed": join_agg_changed,
        "chain_status": {
            "dead_tuples": dead_tuples,
            "stale_statistics": stale_stats,
            "poor_plan_or_join_agg_choice": poor_plan,
            "sort_or_hash_spill": spill,
            "repeated_or_multi_spill": repeated_spill,
        },
    }


def render_report(
    metadata: dict[str, Any],
    chain_summary: dict[str, Any],
    baseline_runs: list[PlanRun],
    anomaly_runs: list[PlanRun],
    output_dir: Path,
) -> str:
    lines = [
        "# PostgreSQL Dead Tuples Chain Report",
        "",
        "## Metadata",
        f"- Output dir: `{output_dir}`",
        f"- Started at: `{metadata['started_at_utc']}`",
        f"- Baseline runs: `{metadata['baseline_runs']}`",
        f"- Query runs: `{metadata['query_runs']}`",
        f"- Inject batch size: `{metadata['inject_batch_size']}`",
        f"- Delete ratio: `{metadata['delete_ratio']}`",
        f"- Requested work_mem: `{metadata['work_mem']}`",
        f"- Effective work_mem: `{metadata.get('effective_work_mem', '')}`",
        f"- Reset environment: `{metadata['reset_environment']}`",
        f"- Force analyze after inject: `{metadata['force_analyze_after_inject']}`",
        "",
        "## Chain Verdict",
    ]

    verdicts = chain_summary["chain_status"]
    for key, value in verdicts.items():
        lines.append(f"- `{key}`: `{'hit' if value else 'miss'}`")

    lines.extend(
        [
            "",
            "## Baseline vs Anomaly",
            f"- Baseline median execution time: `{chain_summary['baseline_execution_time_ms_median']:.2f} ms`",
            f"- Anomaly max execution time: `{chain_summary['anomaly_execution_time_ms_max']:.2f} ms`",
            f"- Join/Agg changed: `{chain_summary['join_agg_changed']}`",
            f"- Baseline max temp bytes: `{chain_summary['baseline_temp_bytes_max']}`",
            f"- Anomaly max temp bytes: `{chain_summary['anomaly_temp_bytes_max']}`",
            "",
            "## Evidence",
            f"- Baseline filtered row count: `{baseline_runs[0].sample['exact_filtered_row_count']}`",
            f"- First anomaly filtered row count: `{anomaly_runs[0].sample['exact_filtered_row_count']}`",
            f"- Baseline n_dead_tup: `{baseline_runs[0].sample['n_dead_tup']}`",
            f"- Post-injection n_dead_tup: `{anomaly_runs[0].sample['n_dead_tup']}`",
            f"- Baseline last_analyze: `{baseline_runs[0].sample['last_analyze']}`",
            f"- Post-injection last_analyze: `{anomaly_runs[0].sample['last_analyze']}`",
            f"- First anomaly misestimation factor: `{anomaly_runs[0].summary['max_misestimation_factor']}`",
            f"- First anomaly sort spill nodes: `{len(anomaly_runs[0].summary['sort_spill_nodes'])}`",
            f"- First anomaly hash spill nodes: `{len(anomaly_runs[0].summary['hash_spill_nodes'])}`",
            f"- First anomaly temp log lines: `{len(anomaly_runs[0].temp_log_lines)}`",
            "",
            "## Notes",
            "- `plans/` contains raw `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` documents.",
            "- `samples.jsonl` contains point-in-time catalog and temp usage snapshots.",
            "- If `--force-analyze-after-inject` was enabled, the chain should break around stale statistics or poor plan detection.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=180, help="Upper bound on wall time, for metadata only.")
    parser.add_argument("--baseline-runs", type=int, default=3)
    parser.add_argument("--inject-batch-size", type=int, default=120000)
    parser.add_argument("--delete-ratio", type=float, default=0.25)
    parser.add_argument("--query-runs", type=int, default=4)
    parser.add_argument("--sample-interval-sec", type=float, default=2.0)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--work-mem", choices=ALLOWED_WORK_MEM_VALUES, default=DEFAULT_WORK_MEM)
    parser.add_argument(
        "--reset-environment",
        action="store_true",
        help="Recreate the Docker Compose stack and volume before the run.",
    )
    parser.add_argument("--shutdown", action="store_true", help="Stop and remove the Docker Compose stack after the run.")
    parser.add_argument(
        "--force-analyze-after-inject",
        action="store_true",
        help="Negative control: refresh stats immediately after injection.",
    )
    return parser.parse_args()


def main() -> int:
    global COMPOSE_WORK_MEM
    args = parse_args()
    COMPOSE_WORK_MEM = args.work_mem
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / timestamp)
    plans_dir = output_dir / "plans"
    output_dir.mkdir(parents=True, exist_ok=True)
    plans_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "started_at_utc": now_utc(),
        "duration": args.duration,
        "baseline_runs": args.baseline_runs,
        "inject_batch_size": args.inject_batch_size,
        "delete_ratio": args.delete_ratio,
        "query_runs": args.query_runs,
        "sample_interval_sec": args.sample_interval_sec,
        "work_mem": args.work_mem,
        "effective_work_mem": "",
        "reset_environment": args.reset_environment,
        "force_analyze_after_inject": args.force_analyze_after_inject,
        "output_dir": str(output_dir),
        "compose_file": str(COMPOSE_FILE),
    }
    write_json(output_dir / "metadata.json", metadata)

    query = load_query()

    try:
        ensure_environment_up(reset_environment=args.reset_environment)
        metadata["effective_work_mem"] = get_effective_work_mem()
        write_json(output_dir / "metadata.json", metadata)
        baseline_runs: list[PlanRun] = []
        anomaly_runs: list[PlanRun] = []
        samples_path = output_dir / "samples.jsonl"

        for run_index in range(args.baseline_runs):
            since = now_utc()
            plan_run = execute_query_and_collect("baseline", run_index, query, since)
            baseline_runs.append(plan_run)
            write_json(plans_dir / f"baseline_{run_index:02d}.json", plan_run.explain_doc)
            append_jsonl(samples_path, {"phase": "baseline", "run_index": run_index, "sample": plan_run.sample})
            time.sleep(args.sample_interval_sec)

        injection_sample = inject_dead_tuples(args.inject_batch_size, args.delete_ratio)
        append_jsonl(samples_path, {"phase": "injection", "sample": injection_sample})
        maybe_force_analyze(args.force_analyze_after_inject)
        time.sleep(args.sample_interval_sec)

        for run_index in range(args.query_runs):
            since = now_utc()
            plan_run = execute_query_and_collect("anomaly", run_index, query, since)
            anomaly_runs.append(plan_run)
            write_json(plans_dir / f"anomaly_{run_index:02d}.json", plan_run.explain_doc)
            append_jsonl(samples_path, {"phase": "anomaly", "run_index": run_index, "sample": plan_run.sample})
            append_jsonl(
                output_dir / "temp_log_lines.jsonl",
                {
                    "phase": "anomaly",
                    "run_index": run_index,
                    "temp_log_lines": plan_run.temp_log_lines,
                },
            )
            time.sleep(args.sample_interval_sec)

        chain_summary = summarize_chain(baseline_runs, anomaly_runs, injection_sample)
        chain_summary["work_mem"] = args.work_mem
        chain_summary["effective_work_mem"] = metadata["effective_work_mem"]
        chain_summary["reset_environment"] = args.reset_environment
        write_json(output_dir / "chain_summary.json", chain_summary)
        report = render_report(metadata, chain_summary, baseline_runs, anomaly_runs, output_dir)
        (output_dir / "report.md").write_text(report, encoding="utf-8")

        print(report)
        return 0
    finally:
        maybe_shutdown(args.shutdown)


if __name__ == "__main__":
    sys.exit(main())
