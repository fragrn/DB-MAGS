#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import RuntimeConfig
from agent_runtime.database_clone import clone_database, drop_database, unique_database_name
from agent_runtime.db import db_cursor
from agent_runtime.evaluator import Evaluator
from agent_runtime.inspector import OneShotEnvironmentInspector
from agent_runtime.memory import MemoryStore
from agent_runtime.reflection import SelfReflectionAgent
from agent_runtime.report import ReportGenerator
from agent_runtime.runtime import build_components
from agent_runtime.safety import SafetyChecker
from agent_runtime.task_dag import TaskDAGBuilder
from agent_runtime.verifier import ProbeProfile, ProbeWorkloadVerifier
from agent_runtime.skills.chaosblade import ChaosBladeInjectionSkill
from agent_runtime.skills.injection_bridge import RunInjectionSkill
from agent_runtime.skills.sql_explain import ExplainSQLSkill
from agent_runtime.types import ExperimentRequest, TaskSpec


CATEGORY_ORDER = [
    "slow_sql",
    "traffic_surge",
    "resource_bottleneck",
    "lock_conflict",
    "database_backup",
]

COMPARISON_SUBTYPES = {"missing_index", "single_sql", "overall_workload", "cpu", "record_lock"}
NUMERIC_TYPES = {"int", "integer", "bigint", "smallint", "mediumint", "tinyint", "decimal", "float", "double", "real"}

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
    parser = argparse.ArgumentParser(description="Run the anomaly suite or a composite multi-anomaly experiment against TPCC databases.")
    parser.add_argument("--db-base", default="dbmags_tpcc_base")
    parser.add_argument("--db-copy", default="dbmags_tpcc_copy")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--anomalies", default="", help="Optional comma-separated subtype filter for the legacy suite runner.")
    parser.add_argument("--subtypes", default="", help="Comma-separated subtype list for single or manual multi-anomaly mode.")
    parser.add_argument("--single", action="store_true", help="Run the new single-anomaly path and preserve one-subtype injection behavior.")
    parser.add_argument("--auto-multi", action="store_true", help="Let GlobalPlannerAgent automatically choose a complex multi-anomaly combination.")
    parser.add_argument("--test", action="store_true", help="Run TPCC probe workload before/after anomaly activation.")
    parser.add_argument("--keep-db", action="store_true", help="Keep the fresh temporary database for debugging.")
    parser.add_argument("--window", type=int, default=12, help="Execution window for single/multi injection flows.")
    parser.add_argument("--risk", default="medium", help="Risk level passed into the planner.")
    parser.add_argument("--target-anomaly", default="", help="High-level target anomaly name for causal-chain runs.")
    parser.add_argument("--target-mode", default="single_root", choices=["single_root", "multi_root", "causal_chain", "causal_subgraph"], help="Target reproduction mode.")
    parser.add_argument("--target-chain", default="", help="Comma-separated causal chain nodes, e.g. traffic_surge,connections_up,lock_contention,slow_query,qps_drop.")
    parser.add_argument("--max-retry-rounds", type=int, default=5, help="Maximum reflexion/replan rounds for --test flows.")
    parser.add_argument("--reward-threshold", type=float, default=0.7, help="Minimum evaluator reward score for success.")
    parser.add_argument("--memory-path", default=str(ROOT / "experiment_runs" / "agent_memory" / "memory.jsonl"))
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


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * fraction)), 0), len(ordered) - 1)
    return ordered[index]


def safe_ratio_delta(post: float | None, baseline: float | None) -> float | None:
    if post is None or baseline in (None, 0):
        return None
    return (post - baseline) / baseline


def stringify_explain_row(row: Any) -> str:
    if isinstance(row, dict):
        return " | ".join(f"{key}={value}" for key, value in row.items())
    if isinstance(row, (list, tuple)):
        return " | ".join(str(item) for item in row)
    return str(row)


def explain_evidence(explain_rows: list[Any]) -> list[str]:
    evidence: list[str] = []
    joined = " ".join(stringify_explain_row(row).lower() for row in explain_rows)
    if "filesort" in joined:
        evidence.append("using filesort")
    if "temporary" in joined:
        evidence.append("using temporary")
    if "all" in joined:
        evidence.append("type=ALL")
    if "skip scan" in joined:
        evidence.append("skip scan")
    if not evidence and explain_rows:
        evidence.append("explain captured")
    return evidence


def summarize_sql_probe(database: str, sql: str, latencies_ms: list[float], errors: list[str], explain_payload: Dict[str, Any]) -> Dict[str, Any]:
    total_elapsed = sum(latencies_ms) / 1000.0
    successes = len(latencies_ms)
    failures = len(errors)
    return {
        "database": database,
        "probe_sql": sql,
        "elapsed_seconds": total_elapsed,
        "successful_transactions": successes,
        "failed_transactions": failures,
        "qps": successes / total_elapsed if total_elapsed > 0 else 0.0,
        "avg_latency_ms": sum(latencies_ms) / successes if successes else 0.0,
        "p95_latency_ms": percentile(latencies_ms, 0.95),
        "p99_latency_ms": percentile(latencies_ms, 0.99),
        "single_sql_mean_ms": sum(latencies_ms) / successes if successes else 0.0,
        "errors": errors,
        "db_evidence": {
            "explain": explain_payload,
            "explain_features": explain_evidence(explain_payload.get("rows", [])) if explain_payload.get("validated") else [],
        },
    }


def run_sql_probe(database: str, sql: str, repeats: int = 5, capture_explain: bool = True) -> Dict[str, Any]:
    runner = RunInjectionSkill()
    explainer = ExplainSQLSkill()
    latencies_ms: list[float] = []
    errors: list[str] = []
    explain_payload = explainer.execute(sql, database=database) if capture_explain else {"validated": False, "rows": []}
    for _ in range(repeats):
        result = runner.execute({"kind": "sql", "sql": sql, "database": database})
        if result.get("executed"):
            latencies_ms.append(float(result.get("latency_ms", 0.0)))
        else:
            errors.append(str(result.get("error", "sql probe failed")))
    return summarize_sql_probe(database, sql, latencies_ms, errors, explain_payload)


def run_workload_probe(database: str, duration_seconds: int, sleep_time: float, thread_count: int, sql: str = "") -> Dict[str, Any]:
    runner = RunInjectionSkill()
    result = runner.execute(
        {
            "kind": "workload_profile",
            "database": database,
            "duration_seconds": duration_seconds,
            "sleep_time": sleep_time,
            "thread_count": thread_count,
            "sql": sql,
        }
    )
    result.setdefault("probe_sql", sql)
    result.setdefault("db_evidence", {})
    return result


def extract_table_name(sql: str) -> str | None:
    match = re.search(r"\bfrom\s+`?([a-zA-Z0-9_]+)`?", sql, flags=re.IGNORECASE)
    return match.group(1) if match else None


def extract_filter_column(sql: str) -> str | None:
    match = re.search(r"\bwhere\s+`?([a-zA-Z0-9_]+)`?\s*(?:>=|>|=|<|<=|in\b|is\b)", sql, flags=re.IGNORECASE)
    return match.group(1) if match else None


def build_missing_index_baseline_sql(sql: str, context) -> str:
    table_name = extract_table_name(sql)
    anomaly_column = extract_filter_column(sql)
    if not table_name:
        return "SELECT 1"
    table = next((item for item in context.tables if item.name == table_name), None)
    if table is None:
        return f"SELECT COUNT(*) FROM {table_name}"
    indexed_candidate = next((column for column in table.columns if column.indexed and column.name != anomaly_column), None)
    if indexed_candidate is None:
        return f"SELECT COUNT(*) FROM {table_name}"
    if indexed_candidate.data_type.lower() in NUMERIC_TYPES:
        predicate = f"{indexed_candidate.name} > 0"
    else:
        predicate = f"{indexed_candidate.name} IS NOT NULL"
    return f"SELECT COUNT(*) FROM {table_name} WHERE {predicate};"


def cleanup_sql(database: str, statements: list[str]) -> None:
    for statement in statements:
        try:
            with db_cursor(database=database) as (conn, cur):
                cur.execute(statement)
                conn.commit()
        except Exception:
            pass


def extract_base_table_from_join(sql: str) -> str:
    match = re.search(r"\bfrom\s+([a-zA-Z0-9_]+)\s+[a-zA-Z0-9_]+", sql, flags=re.IGNORECASE)
    return match.group(1) if match else "orders"


def create_control_copy(database: str, source_table: str, control_table: str) -> None:
    cleanup_sql(database, [f"DROP TABLE IF EXISTS {control_table}"])
    with db_cursor(database=database) as (conn, cur):
        cur.execute(f"CREATE TABLE {control_table} AS SELECT * FROM {source_table}")
        conn.commit()


def create_excessive_index_control(database: str, control_table: str) -> None:
    cleanup_sql(database, [f"DROP TABLE IF EXISTS {control_table}"])
    with db_cursor(database=database) as (conn, cur):
        cur.execute(
            f"CREATE TABLE {control_table} AS "
            "SELECT o_id, o_c_id, o_carrier_id, o_ol_cnt FROM orders LIMIT 50000"
        )
        conn.commit()


def row_count(database: str, table_name: str) -> int | None:
    try:
        with db_cursor(database=database) as (conn, cur):
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            value = cur.fetchone()
            conn.commit()
        return int(value[0]) if value else 0
    except Exception:
        return None


def workload_probe_defaults(spec: dict[str, Any]) -> tuple[float, int]:
    constraints = spec.get("constraints", {})
    return (
        float(constraints.get("baseline_sleep", 0.044)),
        int(constraints.get("baseline_threads", 40)),
    )


def run_backup_with_workload(database: str, source_table: str, backup_table: str, duration_seconds: int, sleep_time: float, thread_count: int) -> Dict[str, Any]:
    runner = RunInjectionSkill()
    backup_result: Dict[str, Any] = {}
    source_row_count_before = row_count(database, source_table)

    def backup_job() -> None:
        drop_step = {"kind": "sql", "sql": f"DROP TABLE IF EXISTS {backup_table}", "database": database}
        create_step = {"kind": "sql", "sql": f"CREATE TABLE {backup_table} AS SELECT * FROM {source_table}", "database": database}
        drop_out = runner.execute(drop_step)
        create_out = runner.execute(create_step)
        backup_result.update(
            {
                "drop": drop_out,
                "create": create_out,
                "backup_row_count_at_create": row_count(database, backup_table),
                "source_row_count_at_create": row_count(database, source_table),
            }
        )

    thread = threading.Thread(target=backup_job)
    thread.start()
    workload = run_workload_probe(
        database=database,
        duration_seconds=duration_seconds,
        sleep_time=sleep_time,
        thread_count=thread_count,
        sql="",
    )
    thread.join(timeout=max(duration_seconds + 10, 20))
    backup_row_count = backup_result.get("backup_row_count_at_create")
    source_row_count = backup_result.get("source_row_count_at_create")
    workload.setdefault("db_evidence", {})
    workload["db_evidence"]["backup"] = {
        "backup_table": backup_table,
        "source_table": source_table,
        "source_row_count_before": source_row_count_before,
        "backup_row_count": backup_row_count,
        "source_row_count": source_row_count,
        "create_executed": bool(backup_result.get("create", {}).get("executed")),
    }
    cleanup_sql(database, [f"DROP TABLE IF EXISTS {backup_table}"])
    workload["comparison_mode"] = "verification_replay"
    return workload


def run_table_lock_probe(database: str, hold_step: Dict[str, Any], probe_sql: str) -> Dict[str, Any]:
    runner = RunInjectionSkill()
    holder_result: Dict[str, Any] = {}

    def hold_lock() -> None:
        holder_result.update(runner.execute(dict(hold_step)))

    thread = threading.Thread(target=hold_lock)
    thread.start()
    time.sleep(0.2)
    blocked_result = runner.execute({"kind": "sql", "sql": probe_sql, "database": database})
    thread.join(timeout=max(float(hold_step.get("hold_seconds", 5)) + 2.0, 7.0))
    blocked_result.setdefault("db_evidence", {})
    blocked_result["db_evidence"]["lock_holder"] = {
        "executed": holder_result.get("executed", False),
        "hold_seconds": hold_step.get("hold_seconds", 5),
        "lock_kind": "table_lock",
    }
    blocked_result["comparison_mode"] = "verification_replay"
    blocked_result["single_sql_mean_ms"] = blocked_result.get("latency_ms", 0.0)
    return blocked_result


def run_metadata_lock_probe(database: str, alter_sql: str, rollback_sql: str, probe_sql: str, hold_seconds: float = 5.0) -> Dict[str, Any]:
    runner = RunInjectionSkill()
    holder_state: Dict[str, Any] = {"executed": False}

    def hold_metadata_lock() -> None:
        try:
            with db_cursor(database=database) as (conn, cur):
                cur.execute("START TRANSACTION")
                cur.execute(probe_sql)
                holder_state["executed"] = True
                time.sleep(hold_seconds)
                cur.execute("ROLLBACK")
                conn.commit()
        except Exception as exc:
            holder_state["error"] = str(exc)

    thread = threading.Thread(target=hold_metadata_lock)
    thread.start()
    time.sleep(0.2)
    ddl_result = runner.execute({"kind": "sql", "sql": alter_sql, "database": database})
    cleanup_sql(database, [rollback_sql])
    thread.join(timeout=max(hold_seconds + 2.0, 8.0))
    ddl_result.setdefault("db_evidence", {})
    ddl_result["db_evidence"]["metadata_lock_holder"] = holder_state
    ddl_result["comparison_mode"] = "verification_replay"
    ddl_result["ddl_latency_ms"] = ddl_result.get("latency_ms", 0.0)
    return ddl_result


def baseline_metrics_for(spec: dict[str, Any], context, task: TaskSpec) -> Dict[str, Any]:
    subtype = spec["subtype"]
    database = str(task.inputs.get("database", ""))
    if subtype == "missing_index":
        control_sql = build_missing_index_baseline_sql(str(task.inputs.get("sql", "")), context)
        metrics = run_sql_probe(database, control_sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "excessive_index":
        control_table = "agent_excessive_index_control"
        create_excessive_index_control(database, control_table)
        sql = str(task.inputs.get("sql", "")).replace("agent_excessive_index", control_table)
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=False)
        metrics["comparison_mode"] = "control_sql"
        metrics.setdefault("db_evidence", {})
        metrics["db_evidence"]["control_table"] = control_table
        cleanup_sql(database, [f"DROP TABLE IF EXISTS {control_table}"])
        return metrics
    if subtype == "implicit_conversion":
        sql = "SELECT COUNT(*) FROM agent_implicit_conversion_support WHERE customer_id_int = 12345;"
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "multi_table_join":
        sql = (
            "SELECT COUNT(*) FROM orders o "
            "JOIN order_line ol ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id "
            "WHERE o.o_carrier_id IS NOT NULL;"
        )
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "order_by":
        sql = "SELECT * FROM agent_order_by_support LIMIT 5000;"
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "group_by":
        sql = "SELECT SUM(ol_amount) FROM agent_group_by_support;"
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "large_table_scan":
        sql = "SELECT COUNT(*) FROM customer WHERE c_w_id = 1 AND c_d_id = 1 AND c_id = 1;"
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "control_sql"
        return metrics
    if subtype == "single_sql":
        baseline_sleep = float(spec.get("constraints", {}).get("baseline_sleep", 0.02))
        baseline_threads = int(spec.get("constraints", {}).get("baseline_threads", 12))
        return run_workload_probe(
            database=database,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
            sql=str(task.inputs.get("sql", "")),
        )
    if subtype == "overall_workload":
        baseline_sleep = float(spec.get("constraints", {}).get("baseline_sleep", 0.044))
        baseline_threads = int(spec.get("constraints", {}).get("baseline_threads", 40))
        return run_workload_probe(
            database=database,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
            sql="",
        )
    if subtype in {"cpu", "io", "network", "memory", "disk"}:
        baseline_sleep, baseline_threads = workload_probe_defaults(spec)
        return run_workload_probe(
            database=database,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
            sql="",
        )
    if subtype == "record_lock":
        step = task.execution_steps[0]
        sql = str(step.get("sql", ""))
        baseline = run_sql_probe(database, sql, repeats=1, capture_explain=False)
        baseline["comparison_mode"] = "no_lock"
        return baseline
    if subtype == "table_lock":
        probe_sql = "SELECT COUNT(*) FROM new_orders WHERE no_w_id = 1 AND no_d_id = 1 AND no_o_id > 10;"
        baseline = run_sql_probe(database, probe_sql, repeats=1, capture_explain=False)
        baseline["comparison_mode"] = "no_lock"
        return baseline
    if subtype == "metadata_lock":
        step = task.execution_steps[0]
        rollback_sql = task.rollback_steps[0]["sql"] if task.rollback_steps else ""
        baseline = run_sql_probe(database, str(step.get("sql", "")), repeats=1, capture_explain=False)
        baseline["comparison_mode"] = "no_lock"
        baseline["ddl_latency_ms"] = baseline.get("single_sql_mean_ms", 0.0)
        cleanup_sql(database, [rollback_sql] if rollback_sql else [])
        return baseline
    if subtype == "database_table_backup":
        baseline_sleep, baseline_threads = workload_probe_defaults(spec)
        return run_workload_probe(
            database=database,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
            sql="",
        )
    return {
        "status": "not_implemented",
        "reason": f"baseline comparison is not implemented for {subtype}",
    }


def post_metrics_for(spec: dict[str, Any], task: TaskSpec, task_results: list[Any], context) -> Dict[str, Any]:
    subtype = spec["subtype"]
    database = str(task.inputs.get("database", ""))
    if subtype == "missing_index":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype == "excessive_index":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=False)
        metrics["comparison_mode"] = "anomalous_sql"
        metrics.setdefault("db_evidence", {})
        metrics["db_evidence"]["redundant_indexes_present"] = True
        return metrics
    if subtype == "implicit_conversion":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype == "multi_table_join":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype == "order_by":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype == "group_by":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=3, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype == "large_table_scan":
        sql = str(task.inputs.get("sql", ""))
        metrics = run_sql_probe(database, sql, repeats=5, capture_explain=True)
        metrics["comparison_mode"] = "anomalous_sql"
        return metrics
    if subtype in {"single_sql", "overall_workload"}:
        if task_results:
            execution = (task_results[0].artifacts.get("execution") or [])[-1]
            execution["comparison_mode"] = "task_execution"
            execution.setdefault("probe_sql", str(task.inputs.get("sql", "")))
            execution.setdefault("db_evidence", {})
            return execution
    if subtype in {"cpu", "io", "network", "memory", "disk"}:
        chaos = ChaosBladeInjectionSkill()
        injection = chaos.execute(subtype, execute=True)
        baseline_sleep, baseline_threads = workload_probe_defaults(spec)
        workload = run_workload_probe(
            database=database,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
            sql="",
        )
        cleanup = chaos.cleanup(str(injection.get("uid", ""))) if injection.get("uid") else {"cleaned": False, "error": "no uid"}
        workload["resource_injection"] = injection
        workload["resource_cleanup"] = cleanup
        workload.setdefault("db_evidence", {})
        workload["db_evidence"]["chaosblade"] = {
            "executed": injection.get("executed", False),
            "uid": injection.get("uid", ""),
            "cleaned": cleanup.get("cleaned", False),
            "error": injection.get("stderr") or injection.get("stdout") or cleanup.get("error", ""),
        }
        workload["comparison_mode"] = "verification_replay"
        return workload
    if subtype == "record_lock":
        runner = RunInjectionSkill()
        hold_step = dict(task.execution_steps[0])
        holder_result: Dict[str, Any] = {}

        def hold_lock() -> None:
            holder_result.update(runner.execute(hold_step))

        thread = threading.Thread(target=hold_lock)
        thread.start()
        time.sleep(0.2)
        blocked_result = runner.execute({"kind": "sql", "sql": str(hold_step.get("sql", "")), "database": database})
        thread.join(timeout=max(float(hold_step.get("hold_seconds", 5)) + 2.0, 7.0))
        blocked_result.setdefault("db_evidence", {})
        blocked_result["db_evidence"]["lock_holder"] = {
            "executed": holder_result.get("executed", False),
            "hold_seconds": hold_step.get("hold_seconds", 5),
        }
        blocked_result["comparison_mode"] = "verification_replay"
        blocked_result["single_sql_mean_ms"] = blocked_result.get("latency_ms", 0.0)
        return blocked_result
    if subtype == "table_lock":
        probe_sql = "SELECT COUNT(*) FROM new_orders WHERE no_w_id = 1 AND no_d_id = 1 AND no_o_id > 10;"
        return run_table_lock_probe(database, task.execution_steps[0], probe_sql)
    if subtype == "metadata_lock":
        alter_sql = str(task.execution_steps[0].get("sql", ""))
        rollback_sql = str(task.rollback_steps[0].get("sql", "")) if task.rollback_steps else ""
        probe_sql = "SELECT COUNT(*) FROM new_orders WHERE no_w_id = 1 AND no_d_id = 1;"
        return run_metadata_lock_probe(database, alter_sql, rollback_sql, probe_sql, hold_seconds=float(task.execution_steps[0].get("hold_seconds", 5)))
    if subtype == "database_table_backup":
        baseline_sleep, baseline_threads = workload_probe_defaults(spec)
        source_table = str(task.inputs.get("source_table", "orders"))
        backup_table = str(task.inputs.get("backup_table", f"{source_table}_backup_agent"))
        return run_backup_with_workload(
            database=database,
            source_table=source_table,
            backup_table=backup_table,
            duration_seconds=int(task.inputs.get("duration_seconds", spec["window"])),
            sleep_time=baseline_sleep,
            thread_count=baseline_threads,
        )
    return {
        "status": "not_implemented",
        "reason": f"post-injection comparison is not implemented for {subtype}",
    }


def compare_metrics(spec: dict[str, Any], baseline: Dict[str, Any], post: Dict[str, Any]) -> Dict[str, Any]:
    subtype = spec["subtype"]
    if baseline.get("status") == "not_implemented" or post.get("status") == "not_implemented":
        return {
            "comparison_status": "validation_inconclusive",
            "baseline": baseline,
            "post": post,
            "delta": {},
            "db_evidence": {},
            "workload_evidence": {},
            "anomaly_confirmed": False,
            "confirmation_reason": f"comparison not implemented for {subtype}",
        }

    delta = {
        "qps_ratio_delta": safe_ratio_delta(post.get("qps"), baseline.get("qps")),
        "avg_latency_ratio_delta": safe_ratio_delta(post.get("avg_latency_ms"), baseline.get("avg_latency_ms")),
        "p95_latency_ratio_delta": safe_ratio_delta(post.get("p95_latency_ms"), baseline.get("p95_latency_ms")),
        "single_sql_ratio_delta": safe_ratio_delta(post.get("single_sql_mean_ms"), baseline.get("single_sql_mean_ms")),
        "failure_delta": (post.get("failed_transactions") or 0) - (baseline.get("failed_transactions") or 0),
    }
    db_evidence = {
        "baseline": baseline.get("db_evidence", {}),
        "post": post.get("db_evidence", {}),
    }
    workload_evidence = {
        "baseline_qps": baseline.get("qps"),
        "post_qps": post.get("qps"),
        "baseline_p95_latency_ms": baseline.get("p95_latency_ms"),
        "post_p95_latency_ms": post.get("p95_latency_ms"),
        "baseline_failures": baseline.get("failed_transactions"),
        "post_failures": post.get("failed_transactions"),
    }

    confirmed = False
    reason = "comparison collected but evidence was inconclusive"
    comparison_status = "completed"

    if subtype == "network" and not post.get("db_evidence", {}).get("chaosblade", {}).get("executed"):
        comparison_status = "environment_blocked"
        reason = "network injection is blocked in the current environment (likely platform or iptables dependency)"

    if subtype == "missing_index":
        features = post.get("db_evidence", {}).get("explain_features", [])
        latency_worse = (post.get("single_sql_mean_ms") or 0.0) > max((baseline.get("single_sql_mean_ms") or 0.0) * 1.5, 1.0)
        features_hit = any(item in {"type=ALL", "skip scan", "using filesort", "using temporary", "explain captured"} for item in features)
        confirmed = latency_worse and features_hit
        if confirmed:
            reason = "post SQL is slower than baseline control SQL and EXPLAIN shows slow-path evidence"
        else:
            reason = "missing-index probe did not show both slower latency and recognizable EXPLAIN evidence"
    elif subtype in {"single_sql", "overall_workload"}:
        qps_up = (post.get("qps") or 0.0) > (baseline.get("qps") or 0.0)
        latency_up = (post.get("avg_latency_ms") or 0.0) > (baseline.get("avg_latency_ms") or 0.0)
        confirmed = qps_up and latency_up
        if confirmed:
            reason = "post workload shows both throughput increase and latency increase"
        else:
            reason = "post workload changed throughput, but latency did not rise together with it"
    elif subtype == "cpu":
        injection_ok = bool(post.get("db_evidence", {}).get("chaosblade", {}).get("executed"))
        qps_down = (post.get("qps") or 0.0) < (baseline.get("qps") or 0.0)
        latency_up = (post.get("p95_latency_ms") or 0.0) > (baseline.get("p95_latency_ms") or 0.0)
        failures_up = (post.get("failed_transactions") or 0) > (baseline.get("failed_transactions") or 0)
        confirmed = injection_ok and (qps_down or latency_up or failures_up)
        if confirmed:
            reason = "resource injection succeeded and workload degraded under the same probe"
        else:
            reason = "resource injection succeeded, but the verification workload did not show clear degradation"
    elif subtype == "record_lock":
        post_latency = post.get("single_sql_mean_ms") or 0.0
        base_latency = baseline.get("single_sql_mean_ms") or 0.0
        confirmed = post_latency > max(base_latency * 3.0, 1000.0)
        if confirmed:
            reason = "the same SQL waited much longer when the row-lock holder was active"
        else:
            reason = "lock replay did not make the probe SQL wait long enough to confirm contention"
    elif subtype == "excessive_index":
        confirmed = bool(post.get("db_evidence", {}).get("redundant_indexes_present")) and (post.get("avg_latency_ms") or 0.0) > (baseline.get("avg_latency_ms") or 0.0)
        reason = (
            "redundant-index workload is slower than the control table and redundant indexes are present"
            if confirmed
            else "redundant-index workload did not show enough write overhead compared with the control table"
        )
    elif subtype == "implicit_conversion":
        post_features = post.get("db_evidence", {}).get("explain_features", [])
        confirmed = (post.get("single_sql_mean_ms") or 0.0) > (baseline.get("single_sql_mean_ms") or 0.0) and bool(post_features)
        reason = (
            "implicit-conversion SQL is slower than the typed control SQL and EXPLAIN captured degraded access"
            if confirmed
            else "implicit-conversion SQL did not show a clear slowdown or EXPLAIN degradation"
        )
    elif subtype == "multi_table_join":
        confirmed = (post.get("single_sql_mean_ms") or 0.0) > (baseline.get("single_sql_mean_ms") or 0.0) * 1.2
        reason = (
            "the wider join is slower than the two-table control join"
            if confirmed
            else "the multi-table join did not widen enough relative to the control join"
        )
    elif subtype == "order_by":
        post_features = post.get("db_evidence", {}).get("explain_features", [])
        confirmed = "using filesort" in post_features and (post.get("single_sql_mean_ms") or 0.0) > (baseline.get("single_sql_mean_ms") or 0.0)
        reason = (
            "ordered query is slower than the control scan and EXPLAIN shows filesort"
            if confirmed
            else "order-by probe did not show both slowdown and filesort evidence"
        )
    elif subtype == "group_by":
        post_features = post.get("db_evidence", {}).get("explain_features", [])
        feature_hit = any(item in {"using temporary", "using filesort"} for item in post_features)
        confirmed = feature_hit and (post.get("single_sql_mean_ms") or 0.0) > (baseline.get("single_sql_mean_ms") or 0.0)
        reason = (
            "group-by query is slower than the aggregate control and EXPLAIN shows temporary/filesort work"
            if confirmed
            else "group-by probe did not show both slowdown and temporary/filesort evidence"
        )
    elif subtype == "large_table_scan":
        post_features = post.get("db_evidence", {}).get("explain_features", [])
        confirmed = any(item in {"type=ALL", "explain captured"} for item in post_features) and (post.get("single_sql_mean_ms") or 0.0) > (baseline.get("single_sql_mean_ms") or 0.0) * 1.2
        reason = (
            "large-table scan is slower than the point lookup control and EXPLAIN shows scan-heavy access"
            if confirmed
            else "large-table scan probe did not show both slowdown and scan-heavy evidence"
        )
    elif subtype in {"io", "memory", "disk"}:
        injection_ok = bool(post.get("db_evidence", {}).get("chaosblade", {}).get("executed"))
        qps_down = (post.get("qps") or 0.0) < (baseline.get("qps") or 0.0)
        latency_up = (post.get("p95_latency_ms") or 0.0) > (baseline.get("p95_latency_ms") or 0.0)
        failures_up = (post.get("failed_transactions") or 0) > (baseline.get("failed_transactions") or 0)
        confirmed = injection_ok and (qps_down or latency_up or failures_up)
        reason = (
            f"{subtype} injection succeeded and the workload probe degraded"
            if confirmed
            else f"{subtype} injection succeeded, but the workload probe did not show clear degradation"
        )
    elif subtype == "table_lock":
        confirmed = bool(post.get("db_evidence", {}).get("lock_holder", {}).get("executed")) and (post.get("single_sql_mean_ms") or 0.0) > max((baseline.get("single_sql_mean_ms") or 0.0) * 3.0, 1000.0)
        reason = (
            "the read probe waited much longer while the WRITE lock was held"
            if confirmed
            else "table-lock replay did not make the read probe wait long enough to confirm contention"
        )
    elif subtype == "metadata_lock":
        confirmed = bool(post.get("db_evidence", {}).get("metadata_lock_holder", {}).get("executed")) and (post.get("ddl_latency_ms") or 0.0) > max((baseline.get("ddl_latency_ms") or 0.0) * 3.0, 1000.0)
        reason = (
            "the ALTER TABLE probe waited much longer while a metadata lock holder was active"
            if confirmed
            else "metadata-lock replay did not delay the DDL enough to confirm contention"
        )
    elif subtype == "database_table_backup":
        backup_info = post.get("db_evidence", {}).get("backup", {})
        backup_rows = backup_info.get("backup_row_count")
        source_rows = backup_info.get("source_row_count")
        counts_match = (
            isinstance(backup_rows, int)
            and isinstance(source_rows, int)
            and abs(backup_rows - source_rows) <= 10
        )
        workload_changed = (post.get("p95_latency_ms") or 0.0) > (baseline.get("p95_latency_ms") or 0.0) or (post.get("qps") or 0.0) < (baseline.get("qps") or 0.0)
        confirmed = counts_match
        if counts_match and workload_changed:
            reason = "backup table was created successfully and the concurrent workload showed measurable impact"
        elif counts_match:
            reason = "backup table was created successfully; workload impact was weak but the backup anomaly was triggered"
        else:
            reason = "backup table creation did not complete or the copied row counts did not match"

    return {
        "comparison_status": comparison_status,
        "baseline": baseline,
        "post": post,
        "delta": delta,
        "db_evidence": db_evidence,
        "workload_evidence": workload_evidence,
        "anomaly_confirmed": confirmed,
        "confirmation_reason": reason,
    }


def write_comparison_files(out_dir: Path, baseline: Dict[str, Any], post: Dict[str, Any], comparison: Dict[str, Any]) -> None:
    (out_dir / "baseline_metrics.json").write_text(json.dumps(baseline, ensure_ascii=True, indent=2))
    (out_dir / "post_metrics.json").write_text(json.dumps(post, ensure_ascii=True, indent=2))
    (out_dir / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=True, indent=2))


def placeholder_result(spec: dict[str, Any], database: str, errors: list[str]) -> Dict[str, Any]:
    return {
        "experiment_id": spec["id"],
        "category": spec["category"],
        "subtype": spec["subtype"],
        "database": database,
        "status": "failed",
        "agent": "GlobalPlannerAgent",
        "artifacts": {"tasks": []},
        "errors": errors,
        "comparison": {
            "comparison_status": "failed",
            "anomaly_confirmed": False,
            "confirmation_reason": "planner returned no executable tasks",
        },
    }


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
        "comparison_strategy": "same_script_before_after",
        "experiments": [],
    }
    summary_rows = []
    comparison_rows = []

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
            result = placeholder_result(spec, database, response.follow_up_questions or ["planner returned no executable tasks"])
            (out_dir / "task.json").write_text(json.dumps({"tasks": []}, ensure_ascii=True, indent=2))
            (out_dir / "metrics.json").write_text(json.dumps({"observed_signals": [], "task_count": 0}, ensure_ascii=True, indent=2))
            write_comparison_files(
                out_dir,
                {"status": "failed", "reason": "planner returned no baseline task"},
                {"status": "failed", "reason": "planner returned no post task"},
                result["comparison"],
            )
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
            comparison_rows.append(
                {
                    "experiment_id": spec["id"],
                    "subtype": spec["subtype"],
                    "status": "failed",
                    "comparison_status": "failed",
                    "baseline_qps": "",
                    "post_qps": "",
                    "baseline_p95_ms": "",
                    "post_p95_ms": "",
                    "baseline_failures": "",
                    "post_failures": "",
                    "db_evidence_summary": "planner returned no executable tasks",
                    "anomaly_confirmed": False,
                    "confirmation_reason": "planner returned no executable tasks",
                }
            )
            continue

        (out_dir / "task.json").write_text(json.dumps(to_jsonable({"tasks": response.plan.tasks}), ensure_ascii=True, indent=2))
        baseline = baseline_metrics_for(spec, context, response.plan.tasks[0])
        task_results = components.scheduler.run(response.plan.tasks)
        metrics = {
            "observed_signals": [signal for item in task_results for signal in item.observed_signals],
            "task_count": len(task_results),
            "cleanup_statuses": {item.task_id: item.cleanup_status for item in task_results},
        }
        post = post_metrics_for(spec, response.plan.tasks[0], task_results, context)
        comparison = compare_metrics(spec, baseline, post)
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
            "comparison": {
                "comparison_status": comparison.get("comparison_status"),
                "anomaly_confirmed": comparison.get("anomaly_confirmed"),
                "confirmation_reason": comparison.get("confirmation_reason"),
            },
        }
        write_comparison_files(out_dir, baseline, post, comparison)
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
                "anomaly_confirmed": comparison.get("anomaly_confirmed"),
                "comparison_status": comparison.get("comparison_status"),
                "confirmation_reason": comparison.get("confirmation_reason"),
            }
        )
        comparison_rows.append(
            {
                "experiment_id": spec["id"],
                "subtype": spec["subtype"],
                "status": status,
                "comparison_status": comparison.get("comparison_status"),
                "baseline_qps": baseline.get("qps", ""),
                "post_qps": post.get("qps", ""),
                "baseline_p95_ms": baseline.get("p95_latency_ms", ""),
                "post_p95_ms": post.get("p95_latency_ms", ""),
                "baseline_failures": baseline.get("failed_transactions", ""),
                "post_failures": post.get("failed_transactions", ""),
                "db_evidence_summary": "; ".join(post.get("db_evidence", {}).get("explain_features", [])) if isinstance(post.get("db_evidence"), dict) else "",
                "anomaly_confirmed": comparison.get("anomaly_confirmed"),
                "confirmation_reason": comparison.get("confirmation_reason"),
            }
        )

    (output_root / "suite_summary.json").write_text(json.dumps(suite, ensure_ascii=True, indent=2))
    write_csv(output_root / "plot_ready_summary.csv", summary_rows)
    write_csv(output_root / "comparison_summary.csv", comparison_rows)
    print(str(output_root))
    return 0


class ActiveTaskHandle:
    def __init__(self, task: TaskSpec, thread=None, injection=None, cleanup=None):
        self.task = task
        self.thread = thread
        self.injection = injection or {}
        self.cleanup = cleanup or {}
        self.result = None
        self.error = ""


def parse_subtypes_arg(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def fresh_database_name(base_name: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return unique_database_name(base_name, stamp.lower())


def create_fresh_experiment_database(source_db: str) -> str:
    target = fresh_database_name(source_db)
    clone_database(source_db, target)
    return target


def build_request_for_mode(args, target_database: str) -> ExperimentRequest:
    subtypes = parse_subtypes_arg(args.subtypes)
    if args.single:
        mode = "single"
        if len(subtypes) != 1:
            raise SystemExit("--single requires exactly one subtype via --subtypes")
    elif args.auto_multi:
        mode = "multi_auto"
    elif subtypes:
        mode = "multi_manual"
    else:
        mode = "legacy"
    return ExperimentRequest(
        user_goal=(
            args.target_anomaly
            or "Inject a single anomaly on a fresh TPCC clone"
            if mode == "single"
            else args.target_anomaly
            or "Inject a complex multi-anomaly combination on a fresh TPCC clone"
        ),
        target_anomaly=args.target_anomaly,
        target_mode=args.target_mode,
        target_chain=parse_subtypes_arg(args.target_chain),
        workload_config={"type": "tpcc", "probe_workload": bool(args.test)},
        max_retry_rounds=max(1, args.max_retry_rounds),
        safety_constraints={"max_duration_sec": max(300, max(1, args.window) * max(1, args.max_retry_rounds)), "max_connection_usage_ratio": 0.8, "max_cpu_usage": 90},
        target_database=target_database,
        allowed_anomalies=list(subtypes),
        allowed_subtypes=list(subtypes),
        execution_window_seconds=args.window,
        risk_level=args.risk,
        require_confirmation=False,
        execution_mode="sequential",
        database_topology="base_and_copy",
        mode=mode if mode != "legacy" else "single",
        test_enabled=bool(args.test),
        fresh_database_per_run=True,
        keep_database=bool(args.keep_db),
    )


def activation_profile(task: TaskSpec, request: ExperimentRequest) -> int:
    for step in task.execution_steps:
        if step.get("kind") == "workload_profile":
            return int(step.get("duration_seconds", request.execution_window_seconds + 4))
        if step.get("kind") in {"hold_sql", "hold_metadata_lock", "sql"}:
            return int(step.get("hold_seconds", request.execution_window_seconds + 4))
    return request.execution_window_seconds + 4


def activate_task(task: TaskSpec, runner: RunInjectionSkill, chaos: ChaosBladeInjectionSkill) -> ActiveTaskHandle:
    handle = ActiveTaskHandle(task)
    role = task.task_role
    if role in {"background_anomaly", "lock_holder", "backup_job"}:
        def job():
            try:
                results = []
                for step in task.execution_steps:
                    results.append(runner.execute(step=step))
                handle.result = results
            except Exception as exc:  # pragma: no cover - defensive
                handle.error = str(exc)
        thread = threading.Thread(target=job, daemon=True)
        thread.start()
        handle.thread = thread
        time.sleep(0.2)
        return handle
    if role == "resource_injection":
        result = chaos.execute(task.anomaly_type, execute=True)
        handle.injection = result
        return handle
    results = []
    for step in task.execution_steps:
        results.append(runner.execute(step=step))
    handle.result = results
    return handle


def wait_for_handles(handles: list[ActiveTaskHandle], timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    for handle in handles:
        if handle.thread is not None:
            remaining = max(0.0, deadline - time.time())
            handle.thread.join(timeout=remaining)


def cleanup_handles(handles: list[ActiveTaskHandle], chaos: ChaosBladeInjectionSkill) -> list[dict[str, object]]:
    cleanup_artifacts = []
    for handle in reversed(handles):
        if handle.injection.get("uid"):
            cleanup = chaos.cleanup(str(handle.injection.get("uid", "")))
            handle.cleanup = cleanup
            cleanup_artifacts.append({"task_id": handle.task.task_id, "cleanup": cleanup})
    return cleanup_artifacts


def execution_trace_from_handles(handles: list[ActiveTaskHandle], cleanup_artifacts: list[dict[str, object]]):
    return {
        "task_status": {
            handle.task.task_id: "failed" if handle.error else "completed"
            for handle in handles
        },
        "stdout": {
            handle.task.task_id: handle.result if handle.result is not None else handle.injection
            for handle in handles
        },
        "stderr": {
            handle.task.task_id: handle.error
            for handle in handles
            if handle.error
        },
        "errors": {
            handle.task.task_id: handle.error
            for handle in handles
            if handle.error
        },
        "cleanup_status": {
            item.get("task_id", ""): item.get("cleanup", {})
            for item in cleanup_artifacts
        },
    }


def composite_output_root(args, request: ExperimentRequest, subtypes: list[str]) -> Path:
    if args.output_root:
        root = Path(args.output_root)
    else:
        root = ROOT / "experiment_runs" / "composite_experiments"
    root.mkdir(parents=True, exist_ok=True)
    name = "__".join(subtypes[:5]) if subtypes else "auto_multi"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = root / f"{stamp}_{name}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_composite_or_single(args) -> int:
    components = build_components(RuntimeConfig.from_env())
    inspector = OneShotEnvironmentInspector(components.skills)
    dag_builder = TaskDAGBuilder()
    safety_checker = SafetyChecker()
    evaluator = Evaluator(reward_threshold=args.reward_threshold)
    reflector = SelfReflectionAgent(components.llm_client)
    memory = MemoryStore(args.memory_path)
    reporter = ReportGenerator()
    runner = RunInjectionSkill()
    chaos = ChaosBladeInjectionSkill()
    verifier = ProbeWorkloadVerifier(components.skills)

    out_dir = None
    attempts: list[dict[str, Any]] = []
    final_payload: dict[str, Any] = {}
    max_rounds = args.max_retry_rounds if args.test else 1
    reflection = None
    evaluation = None

    for round_index in range(1, max_rounds + 1):
        fresh_db = create_fresh_experiment_database(args.db_base)
        request = build_request_for_mode(args, fresh_db)
        if reflection and evaluation:
            request = components.planner.replan_from_reflection(request, reflection, evaluation, memory.load_recent())
        context = components.planner.gather_context(request)
        environment_snapshot = inspector.inspect(request, context)
        response = components.planner.plan(request, context)
        if out_dir is None:
            selected = response.planner_decision.selected_anomalies if response.planner_decision else request.allowed_subtypes
            out_dir = composite_output_root(args, request, selected)
            (out_dir / "request.json").write_text(json.dumps(to_jsonable(request), ensure_ascii=True, indent=2))
            (out_dir / "environment_snapshot.json").write_text(json.dumps(to_jsonable(environment_snapshot), ensure_ascii=True, indent=2))
        round_dir = out_dir / f"round_{round_index}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "request.json").write_text(json.dumps(to_jsonable(request), ensure_ascii=True, indent=2))
        (round_dir / "environment_snapshot.json").write_text(json.dumps(to_jsonable(environment_snapshot), ensure_ascii=True, indent=2))
        (round_dir / "plan.json").write_text(json.dumps(to_jsonable(response), ensure_ascii=True, indent=2))

        if response.follow_up_questions or response.plan is None or not response.plan.tasks:
            result = {"status": "failed", "errors": response.follow_up_questions or ["planner returned no tasks"]}
            (round_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
            if not args.keep_db:
                drop_database(fresh_db)
            attempts.append({"round": round_index, "status": "failed", "execution_database": fresh_db, "errors": result["errors"]})
            final_payload = result
            break

        tasks = response.plan.tasks
        task_agent_outputs = dag_builder.task_agent_outputs(tasks)
        task_dag = dag_builder.build(tasks, response.planner_decision)
        safety = safety_checker.check(task_dag, environment_snapshot, request)
        (round_dir / "task_agent_outputs.json").write_text(json.dumps(to_jsonable(task_agent_outputs), ensure_ascii=True, indent=2))
        (round_dir / "task_dag.json").write_text(json.dumps(to_jsonable(task_dag), ensure_ascii=True, indent=2))
        (round_dir / "safety_check.json").write_text(json.dumps(to_jsonable(safety), ensure_ascii=True, indent=2))
        if round_index == 1:
            (out_dir / "global_plan.json").write_text(json.dumps(to_jsonable(response.planner_decision.global_plan if response.planner_decision else {}), ensure_ascii=True, indent=2))
            (out_dir / "task_dag.json").write_text(json.dumps(to_jsonable(task_dag), ensure_ascii=True, indent=2))
            (out_dir / "safety_check.json").write_text(json.dumps(to_jsonable(safety), ensure_ascii=True, indent=2))

        if not safety.approved:
            result = {"status": "rejected_by_safety_checker", "reasons": safety.reasons, "warnings": safety.warnings}
            (round_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
            if not args.keep_db:
                drop_database(fresh_db)
            attempts.append({"round": round_index, "status": result["status"], "execution_database": fresh_db, "safety": to_jsonable(safety)})
            final_payload = result
            break

        baseline = {}
        post = {}
        comparison = {}
        activation_log = []
        handles: list[ActiveTaskHandle] = []
        cleanup_artifacts = []
        execution_trace = {}
        probe_profile = ProbeProfile(database=fresh_db, duration_seconds=max(6, min(args.window, 20)), sleep_time=0.044, thread_count=40)
        try:
            if request.test_enabled:
                baseline = verifier.run_probe(probe_profile, comparison_mode="baseline_tpcc_probe")
            order = response.planner_decision.activation_order if response.planner_decision else [task.anomaly_type for task in tasks]
            by_subtype = {task.anomaly_type: task for task in tasks}
            for subtype in order:
                task = by_subtype.get(subtype)
                if task is None:
                    continue
                handle = activate_task(task, runner, chaos)
                handles.append(handle)
                activation_log.append({
                    "task_id": task.task_id,
                    "anomaly_type": task.anomaly_type,
                    "task_role": task.task_role,
                    "resource_injection": handle.injection,
                })
            if request.test_enabled:
                post = verifier.run_probe(probe_profile, comparison_mode="after_anomaly_probe")
                comparison = verifier.compare(baseline, post)
                evaluation = evaluator.evaluate(request, task_dag, baseline, post, {"safety_violated": False})
                comparison.update({
                    "requested_subtypes": request.allowed_subtypes or (response.planner_decision.selected_anomalies if response.planner_decision else []),
                    "selection_mode": request.mode,
                    "execution_database": fresh_db,
                    "reward": evaluation.reward,
                    "evaluation_success": evaluation.success,
                })
            wait_for_handles(handles, timeout_seconds=args.window + 10)
        finally:
            cleanup_artifacts = cleanup_handles(handles, chaos)
            execution_trace = execution_trace_from_handles(handles, cleanup_artifacts)
            if not args.keep_db:
                drop_database(fresh_db)

        if request.test_enabled and evaluation and not evaluation.success:
            reflection = reflector.reflect(
                {
                    "global_plan": response.planner_decision.global_plan if response.planner_decision else {},
                    "task_outputs": task_agent_outputs,
                    "execution_trace": execution_trace,
                    "baseline_metrics": baseline,
                    "after_metrics": post,
                    "evaluation_result": evaluation,
                }
            )
            memory.append_reflection(
                reflection,
                {
                    "dbms": environment_snapshot.dbms,
                    "workload": request.workload_config.get("type", "tpcc"),
                    "anomaly_type": ",".join(response.planner_decision.selected_anomalies if response.planner_decision else request.allowed_subtypes),
                    "context": request.target_mode,
                    "evidence": evaluation.reward,
                },
            )
        else:
            reflection = None

        result = {
            "status": "completed",
            "selection_mode": request.mode,
            "execution_database": fresh_db,
            "activated_tasks": to_jsonable(activation_log),
            "cleanup": cleanup_artifacts,
            "comparison": comparison if request.test_enabled else {},
        }
        (round_dir / "activation.json").write_text(json.dumps(to_jsonable(activation_log), ensure_ascii=True, indent=2))
        (round_dir / "execution_trace.json").write_text(json.dumps(to_jsonable(execution_trace), ensure_ascii=True, indent=2))
        (round_dir / "cleanup.json").write_text(json.dumps(to_jsonable(cleanup_artifacts), ensure_ascii=True, indent=2))
        (round_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
        if request.test_enabled:
            (round_dir / "baseline_metrics.json").write_text(json.dumps(baseline, ensure_ascii=True, indent=2))
            (round_dir / "after_metrics.json").write_text(json.dumps(post, ensure_ascii=True, indent=2))
            (round_dir / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=True, indent=2))
            (round_dir / "evaluation.json").write_text(json.dumps(to_jsonable(evaluation), ensure_ascii=True, indent=2))
            (round_dir / "reflection.json").write_text(json.dumps(to_jsonable(reflection or {}), ensure_ascii=True, indent=2))
        attempts.append(
            {
                "round": round_index,
                "status": "completed",
                "execution_database": fresh_db,
                "success": bool(evaluation.success) if evaluation else None,
                "reward": evaluation.reward if evaluation else {},
            }
        )
        final_payload = {
            "status": "completed",
            "attempts": attempts,
            "final_round": round_index,
            "success": bool(evaluation.success) if evaluation else True,
            "selection_mode": request.mode,
        }
        if not request.test_enabled or (evaluation and evaluation.success):
            break

    if out_dir is None:
        out_dir = ROOT / "experiment_runs" / "composite_experiments" / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}_failed"
        out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "attempts.json").write_text(json.dumps(to_jsonable(attempts), ensure_ascii=True, indent=2))
    final_report = reporter.build(
        {
            "request": to_jsonable(request) if "request" in locals() else {},
            "environment": environment_snapshot if "environment_snapshot" in locals() else {},
            "global_plan": response.planner_decision.global_plan if "response" in locals() and response.planner_decision else {},
            "task_dag": task_dag if "task_dag" in locals() else {},
            "execution_trace": execution_trace if "execution_trace" in locals() else {},
            "evaluation": evaluation,
            "reflection": reflection,
            "cleanup": cleanup_artifacts if "cleanup_artifacts" in locals() else [],
        }
    )
    (out_dir / "final_report.json").write_text(json.dumps(to_jsonable(final_report), ensure_ascii=True, indent=2))
    (out_dir / "result.json").write_text(json.dumps(to_jsonable(final_payload), ensure_ascii=True, indent=2))
    print(str(out_dir))
    return 0 if final_payload.get("status") == "completed" else 1


def main():
    args = parse_args()
    if args.single or args.auto_multi or args.subtypes:
        return run_composite_or_single(args)
    return legacy_suite_main()


if __name__ == "__main__":
    raise SystemExit(main())
