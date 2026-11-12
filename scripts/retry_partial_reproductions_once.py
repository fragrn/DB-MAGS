#!/usr/bin/env python3
"""Retry previous partial InputAnalysisAgent reproductions once with Codex-improved blueprints."""
from __future__ import annotations

import json
import re
import time
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from InputAnalysisAgent.hitl import RunState, save_state, write_json
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint, ReproductionEvaluation

ROOT = Path("/Users/neo/.codex/worktrees/5f0a/DB-MAGS/post_redo/post_redo1")
SUMMARY = ROOT / "final_summary.json"


def fact(key: str, value: Any, evidence: str, confidence: float = 0.8) -> dict[str, Any]:
    return {"key": key, "value": value, "source": "agent_inference", "evidence": evidence, "confidence": confidence}


def table(name: str, purpose: str, rows: int, notes: str = "") -> dict[str, Any]:
    return {"name": name, "purpose": purpose, "target_rows": rows, "distribution_notes": notes}


def base_blueprint(*, dbms: str, slug: str, database: str, summary: str, mechanism: str, schema_sql: list[str], generation_sql: list[str], analyze_tables: list[str], tables: list[dict[str, Any]], task_specs: list[dict[str, Any]], evaluation_notes: str, max_duration_sec: int = 45, calibration_queries: list[dict[str, Any]] | None = None, workload_spec: dict[str, Any] | None = None, feasibility_level: str = "mechanism") -> dict[str, Any]:
    if dbms == "mysql" and any("generated_numbers" in sql for sql in generation_sql):
        helper_schema = [
            "CREATE TABLE IF NOT EXISTS digits (i INT PRIMARY KEY) ENGINE=InnoDB",
            "CREATE TABLE IF NOT EXISTS generated_numbers (n INT PRIMARY KEY) ENGINE=InnoDB",
        ]
        helper_generation = [
            "INSERT IGNORE INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9)",
            "INSERT IGNORE INTO generated_numbers(n) SELECT n FROM (SELECT a.i + 10*b.i + 100*c.i + 1000*d.i + 10000*e.i + 100000*f.i + 1 AS n FROM digits a CROSS JOIN digits b CROSS JOIN digits c CROSS JOIN digits d CROSS JOIN digits e CROSS JOIN digits f) x WHERE n BETWEEN 1 AND 300000",
        ]
        schema_sql = helper_schema + schema_sql
        generation_sql = helper_generation + generation_sql
    return {
        "incident_spec": {
            "dbms": dbms,
            "dbms_version": None,
            "summary": summary,
            "symptoms": ["synthetic reproduction of the forum incident symptom"],
            "mechanism": mechanism,
            "facts": [
                fact("source_slug", slug, "Derived from the previous partial experiment slug."),
                fact("codex_retry_scope", "one improved retry", "User requested exactly one retry for prior partial runs."),
            ],
            "assumptions": ["Mechanism-level reproduction with synthetic data is acceptable when original production data is unavailable."],
            "unknowns": ["Original production data distribution and hardware are not available."],
            "confidence": 0.76,
        },
        "feasibility": {
            "level": feasibility_level,
            "rationale": "Codex-generated mechanism-level retry based on partial-run artifacts and failure reason.",
            "missing_capabilities": [],
            "unmatched_conditions": [],
            "confidence": 0.72,
        },
        "environment_spec": {"database": database, "dbms": dbms, "isolation": "dedicated synthetic database"},
        "data_spec": {
            "database": database,
            "schema_sql": schema_sql,
            "generation_sql": generation_sql,
            "tables": tables,
            "constraints": {"codex_retry": True, "source_partial_reason": evaluation_notes},
            "analyze_tables": analyze_tables,
            "calibration_queries": calibration_queries or [],
            "scale_strategy": {"initial_rows": 1000, "max_rows": 1000, "growth_factor": 2.0, "max_rounds": 1},
        },
        "workload_spec": workload_spec or {"enabled": False, "method": "none"},
        "evaluation_spec": {
            "validation_criteria": [evaluation_notes],
            "minimum_plan_similarity": 0.4,
            "success_threshold": "symptom and mechanism evidence visible in execution artifacts",
            "evidence": {},
        },
        "experiment_request": {"target_database": database, "max_duration_sec": max_duration_sec, "risk_level": "low"},
        "task_specs": task_specs,
        "dependencies": [],
        "risk_assessment": "low",
        "requires_domain_judgment": False,
        "unresolved_critical_questions": [],
        "rationale": evaluation_notes,
    }


def mysql_sql_task(task_id: str, database: str, sql: str, duration: int = 12, concurrency: int = 2, root: str = "slow_sql") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": root,
        "actions": [{"kind": "raw_sql_workload", "database": database, "sql": sql, "concurrency": concurrency, "duration_sec": duration}],
        "cleanup_actions": [],
        "expected_metrics": {"latency_or_counter_delta": "observable"},
        "success_criteria": {"execution_artifact": "latency/error evidence"},
        "risk_assessment": "low",
        "metadata": {"root_cause": root},
    }


def pg_sql_task(task_id: str, database: str, sql: str, duration: int = 15, concurrency: int = 1, root: str = "slow_sql") -> dict[str, Any]:
    return mysql_sql_task(task_id, database, sql, duration, concurrency, root)


def mysql_tx_task(task_id: str, database: str, scripts: list[dict[str, Any]], duration: int = 20, root: str = "lock_wait") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": root,
        "actions": [{"kind": "raw_transaction_script", "database": database, "scripts": scripts, "duration_sec": duration, "concurrency": 1}],
        "cleanup_actions": [],
        "expected_metrics": {"blocked_or_waiting_transactions": "observable"},
        "success_criteria": {"transaction_scripts_complete": True},
        "risk_assessment": "low",
        "metadata": {"root_cause": root},
    }


def blueprint_for(category: str, slug: str, database: str) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = {"category": category, "slug": slug, "improvement": ""}
    if slug == "17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk":
        analysis["improvement"] = "Force disk temporary tables using a wide GROUP BY/ORDER BY query over TEXT payloads and low session tmp table limits."
        schema = [
            "CREATE TABLE IF NOT EXISTS tmp_disk_src (id INT PRIMARY KEY AUTO_INCREMENT, g1 INT NOT NULL, g2 INT NOT NULL, payload TEXT NOT NULL, KEY idx_g1 (g1)) ENGINE=InnoDB",
        ]
        gen = [
            "INSERT INTO tmp_disk_src (g1,g2,payload) SELECT a.n % 2000, FLOOR(a.n/7) % 2000, RPAD(CONCAT('wide-payload-', a.n), 2048, 'x') FROM generated_numbers a WHERE a.n BETWEEN 1 AND 60000"
        ]
        sql = "SET SESSION tmp_table_size=32768; SET SESSION max_heap_table_size=32768; SELECT g1, g2, COUNT(*), SUM(LENGTH(payload)) AS bytes_seen FROM tmp_disk_src GROUP BY g1, g2 ORDER BY bytes_seen DESC, g1 LIMIT 1000"
        # MySQL cursor cannot execute multi-statements by default; use tx script so SETs and SELECT run on same session.
        task = mysql_tx_task("codex_disk_tmp_table_pressure", database, [{"role": "temp_table_query", "autocommit": True, "concurrency": 2, "steps": [{"sql": "SET SESSION tmp_table_size=32768"}, {"sql": "SET SESSION max_heap_table_size=32768"}, {"sql": "SELECT g1, g2, COUNT(*), SUM(LENGTH(payload)) AS bytes_seen FROM tmp_disk_src GROUP BY g1, g2 ORDER BY bytes_seen DESC, g1 LIMIT 1000"}]}], 25, "disk_temp_table")
        return base_blueprint(dbms="mysql", slug=slug, database=database, summary="MySQL creates many on-disk temporary tables", mechanism="Wide GROUP BY/ORDER BY with low temporary-table memory limit spills to disk temporary tables.", schema_sql=schema, generation_sql=gen, analyze_tables=["tmp_disk_src"], tables=[table("tmp_disk_src", "wide rows and many groups for temp-table spill", 60000, "TEXT payload prevents memory temp-table staying small")], task_specs=[task], evaluation_notes=analysis["improvement"], max_duration_sec=45), analysis

    if slug == "mysql-query-performance-query-schema-indexes":
        analysis["improvement"] = "Fix missing NumbID column and run the original-style range/customer query against an indexed calls table."
        schema = [
            "CREATE TABLE IF NOT EXISTS calls (ID INT PRIMARY KEY AUTO_INCREMENT, CustID INT NOT NULL, DateStart DATETIME NOT NULL, NumbID INT NOT NULL, EffectiveFlag TINYINT NOT NULL, OrigNumber VARCHAR(32), DurationSec INT, KEY idx_cust_date (CustID, DateStart), KEY idx_date (DateStart), KEY idx_numb (NumbID)) ENGINE=InnoDB",
        ]
        gen = [
            "INSERT INTO calls (CustID,DateStart,NumbID,EffectiveFlag,OrigNumber,DurationSec) SELECT 1 + (n % 500), TIMESTAMP('2020-01-01') + INTERVAL (n % 200000) SECOND, 1 + (n % 20000), IF(n % 3=0,1,0), CONCAT('555', LPAD(n % 100000, 5, '0')), n % 3600 FROM generated_numbers WHERE n BETWEEN 1 AND 120000"
        ]
        sql = "SELECT SQL_NO_CACHE ID, DateStart, NumbID, EffectiveFlag, OrigNumber FROM calls WHERE CustID BETWEEN 10 AND 350 AND DateStart BETWEEN '2020-01-01' AND '2020-01-03' ORDER BY DateStart DESC, NumbID LIMIT 5000"
        return base_blueprint(dbms="mysql", slug=slug, database=database, summary="MySQL query performance depends on schema/index shape", mechanism="A range query over a call-log style table becomes expensive when many rows qualify and ordering reads many index/table entries.", schema_sql=schema, generation_sql=gen, analyze_tables=["calls"], tables=[table("calls", "call-log table with NumbID column present", 120000, "hundreds of customers and broad date range")], task_specs=[mysql_sql_task("codex_calls_query_with_numbid", database, sql, 15, 3, "slow_sql")], evaluation_notes=analysis["improvement"], max_duration_sec=45), analysis

    if slug == "20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps":
        analysis["improvement"] = "Use high-concurrency sleeping sessions to create connection pressure and expose too-many-connections style failures without changing max_connections."
        schema = ["CREATE TABLE IF NOT EXISTS connection_probe (id INT PRIMARY KEY) ENGINE=InnoDB", "INSERT INTO connection_probe VALUES (1)"]
        gen: list[str] = []
        sql = "SELECT SLEEP(2) FROM connection_probe"
        return base_blueprint(dbms="mysql", slug=slug, database=database, summary="MySQL too many connections under connection pressure", mechanism="Many concurrent sessions hold server connections, reducing available connection headroom and producing connection/latency failures.", schema_sql=schema, generation_sql=gen, analyze_tables=["connection_probe"], tables=[table("connection_probe", "tiny table used to hold sleeping sessions", 1)], task_specs=[mysql_sql_task("codex_connection_pressure_sleep", database, sql, 12, 220, "too_many_connections")], evaluation_notes=analysis["improvement"], max_duration_sec=35), analysis

    if slug == "222262-performance-of-large-transactions-and-concurrency":
        analysis["improvement"] = "Avoid expected deadlock failure; reproduce long-transaction concurrency by making waiters block behind a holder and then complete."
        schema = [
            "CREATE TABLE IF NOT EXISTS tools (id INT PRIMARY KEY, status INT NOT NULL, filler TEXT)"
        ]
        gen = ["INSERT INTO tools SELECT n, 0, repeat('x', 100) FROM generate_series(1, 2000) AS n"]
        scripts = [
            {"role": "holder", "autocommit": False, "concurrency": 1, "steps": ["BEGIN", "UPDATE tools SET status=status+1 WHERE id BETWEEN 1 AND 200", {"sleep_sec": 8}, "COMMIT"]},
            {"role": "waiter", "autocommit": False, "concurrency": 4, "steps": [{"sleep_sec": 1}, "BEGIN", "UPDATE tools SET status=status+1 WHERE id BETWEEN 1 AND 200", "COMMIT"]},
        ]
        return base_blueprint(dbms="postgresql", slug=slug, database=database, summary="Large transactions and concurrent writers suffer lock waits", mechanism="A long transaction holds row locks while concurrent writers wait, causing slow concurrent transaction completion.", schema_sql=schema, generation_sql=gen, analyze_tables=["tools"], tables=[table("tools", "rows updated by holder and waiters", 2000, "same hot row range")], task_specs=[mysql_tx_task("codex_pg_long_tx_lock_wait", database, scripts, 25, "long_transaction_lock_wait")], evaluation_notes=analysis["improvement"], max_duration_sec=45), analysis

    if slug == "108454-postgres-4x-slower-than-it-was":
        analysis["improvement"] = "Use PostgreSQL session-level low work_mem and a large sort/aggregation workload as a mechanism-level substitute for resource-related slowdown."
        schema = ["CREATE TABLE IF NOT EXISTS pg_slow_events (id INT PRIMARY KEY, tenant_id INT, group_id INT, payload TEXT)"]
        gen = ["INSERT INTO pg_slow_events SELECT n, n % 100, n % 50000, repeat(md5(n::text), 8) FROM generate_series(1, 250000) AS n"]
        sql = "SET work_mem='64kB'; SELECT group_id, count(*), max(payload) FROM pg_slow_events GROUP BY group_id ORDER BY max(payload) DESC LIMIT 1000"
        task = mysql_tx_task("codex_pg_low_work_mem_sort", database, [{"role": "low_work_mem_sort", "autocommit": True, "concurrency": 2, "steps": [{"sql": "SET work_mem='64kB'"}, {"sql": "SELECT group_id, count(*), max(payload) FROM pg_slow_events GROUP BY group_id ORDER BY max(payload) DESC LIMIT 1000"}]}], 35, "resource_slowdown")
        return base_blueprint(dbms="postgresql", slug=slug, database=database, summary="PostgreSQL query workload becomes much slower under resource pressure/configuration", mechanism="Constrained work_mem makes a large sort/aggregate spill or run slowly, reproducing the slowdown symptom at mechanism level.", schema_sql=schema, generation_sql=gen, analyze_tables=["pg_slow_events"], tables=[table("pg_slow_events", "large table for sort/aggregate", 250000, "many groups and wide payload")], task_specs=[task], evaluation_notes=analysis["improvement"], max_duration_sec=60), analysis

    if slug == "how-to-optimize-very-slow-select-with-left-joins-over-big-tables":
        analysis["improvement"] = "Make the EAV-style LEFT JOIN query actually execute in the anomaly task with moderate synthetic scale."
        schema = [
            "CREATE TABLE IF NOT EXISTS person (id INT PRIMARY KEY AUTO_INCREMENT, status INT NOT NULL, created_at DATETIME NOT NULL) ENGINE=InnoDB",
            "CREATE TABLE IF NOT EXISTS person_attr (person_id INT NOT NULL, attr_name VARCHAR(32) NOT NULL, attr_value VARCHAR(128) NOT NULL, KEY idx_attr_name_value (attr_name, attr_value, person_id), KEY idx_person_attr (person_id, attr_name)) ENGINE=InnoDB",
        ]
        gen = [
            "INSERT INTO person (status,created_at) SELECT n % 5, TIMESTAMP('2020-01-01') + INTERVAL n SECOND FROM generated_numbers WHERE n BETWEEN 1 AND 50000",
            "INSERT INTO person_attr (person_id,attr_name,attr_value) SELECT p.id, 'first_name', CONCAT('first', p.id % 1000) FROM person p",
            "INSERT INTO person_attr (person_id,attr_name,attr_value) SELECT p.id, 'last_name', CONCAT('last', p.id % 2000) FROM person p",
            "INSERT INTO person_attr (person_id,attr_name,attr_value) SELECT p.id, 'email', CONCAT('user', p.id % 5000, '@example.com') FROM person p",
            "INSERT INTO person_attr (person_id,attr_name,attr_value) SELECT p.id, 'city', CONCAT('city', p.id % 300) FROM person p",
        ]
        sql = "SELECT SQL_NO_CACHE p.id, fn.attr_value AS first_name, ln.attr_value AS last_name, em.attr_value AS email, ct.attr_value AS city FROM person p LEFT JOIN person_attr fn ON fn.person_id=p.id AND fn.attr_name='first_name' LEFT JOIN person_attr ln ON ln.person_id=p.id AND ln.attr_name='last_name' LEFT JOIN person_attr em ON em.person_id=p.id AND em.attr_name='email' LEFT JOIN person_attr ct ON ct.person_id=p.id AND ct.attr_name='city' WHERE p.status IN (1,2,3) AND (ln.attr_value LIKE 'last1%' OR em.attr_value LIKE 'user2%') ORDER BY p.created_at DESC LIMIT 5000"
        return base_blueprint(dbms="mysql", slug=slug, database=database, summary="Slow SELECT with many LEFT JOINs over large EAV-style tables", mechanism="Multiple LEFT JOINs against an attribute table plus broad filters cause many index probes and high latency.", schema_sql=schema, generation_sql=gen, analyze_tables=["person", "person_attr"], tables=[table("person", "base entities", 50000), table("person_attr", "EAV attributes joined repeatedly", 200000)], task_specs=[mysql_sql_task("codex_eav_left_join_slow_query", database, sql, 18, 2, "multi_left_join_slow_sql")], evaluation_notes=analysis["improvement"], max_duration_sec=50), analysis

    if slug == "297892-query-slow-when-a-sub-select-is-used":
        analysis["improvement"] = "Use a PostgreSQL repeated correlated subselect query over synthetic orders to produce visible repeated inner scans."
        schema = ["CREATE TABLE IF NOT EXISTS sub_orders (id INT PRIMARY KEY, customer_id INT, amount INT, created_at TIMESTAMP)", "CREATE INDEX idx_sub_orders_customer ON sub_orders(customer_id)"]
        gen = ["INSERT INTO sub_orders SELECT n, n % 5000, (n % 1000), TIMESTAMP '2020-01-01' + (n || ' seconds')::interval FROM generate_series(1, 200000) AS n"]
        sql = "SELECT o.customer_id, (SELECT count(*) FROM sub_orders s WHERE s.customer_id=o.customer_id AND s.amount > 100) AS high_cnt, (SELECT avg(amount) FROM sub_orders s WHERE s.customer_id=o.customer_id) AS avg_amount, (SELECT max(created_at) FROM sub_orders s WHERE s.customer_id=o.customer_id) AS last_seen FROM (SELECT DISTINCT customer_id FROM sub_orders LIMIT 2500) o ORDER BY high_cnt DESC LIMIT 1000"
        return base_blueprint(dbms="postgresql", slug=slug, database=database, summary="Query becomes slow when a sub-select is used", mechanism="Repeated correlated subqueries scan the same table per outer customer, causing repeated work and slow execution.", schema_sql=schema, generation_sql=gen, analyze_tables=["sub_orders"], tables=[table("sub_orders", "orders/events table used by repeated subselects", 200000, "5000 customers, repeated correlated subqueries")], task_specs=[pg_sql_task("codex_repeated_correlated_subselect", database, sql, 25, 1, "correlated_subselect_slow_sql")], evaluation_notes=analysis["improvement"], max_duration_sec=50), analysis

    raise KeyError(f"no retry blueprint for {slug}")


def next_attempt_dir(post_dir: Path) -> Path:
    max_n = 0
    for p in post_dir.glob("attempt_codex_*"):
        if p.is_dir():
            m = re.fullmatch(r"attempt_codex_(\d+)", p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    out = post_dir / f"attempt_codex_{max_n + 1}"
    out.mkdir(parents=False, exist_ok=False)
    return out


def status_from_eval(evaluation: dict[str, Any]) -> str:
    if evaluation.get("success"):
        return "success"
    if evaluation.get("symptom_hit") or evaluation.get("mechanism_hit") or float(evaluation.get("plan_similarity") or 0) >= 0.4:
        return "partial"
    return "failed_evaluation"


def run_one(runtime: ReproductionRuntime, item: dict[str, Any]) -> dict[str, Any]:
    category = item["category"]
    slug = item["slug"]
    post_dir = Path(item.get("post_dir") or (ROOT / category / slug))
    attempt_dir = next_attempt_dir(post_dir)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "_codex_partial_retry"
    run_dir = attempt_dir / run_id
    run_dir.mkdir(parents=False, exist_ok=False)
    database = "codex_retry_" + re.sub(r"[^a-zA-Z0-9_]", "_", slug)[:42].strip("_")
    blueprint_payload, analysis = blueprint_for(category, slug, database)
    analysis.update({
        "source_post_dir": str(post_dir),
        "attempt_dir": str(attempt_dir),
        "run_dir": str(run_dir),
        "previous_final_status": item.get("final_status"),
        "timestamp": time.time(),
    })
    write_json(run_dir / "codex_partial_analysis.json", analysis)
    write_json(run_dir / "candidate_blueprint.json", blueprint_payload)
    state = RunState(run_id=run_id, status="running", phase="planning", interaction="checkpoint")
    save_state(run_dir, state)
    try:
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload)
        runtime._validate_blueprint_safety(blueprint)
        runtime._write_blueprint_artifacts(run_dir, blueprint)
        state.artifacts["blueprint"] = "blueprint.json"
        runtime._complete_phase(run_dir, state, "planning")
        prep = runtime._prepare_database(blueprint)
        write_json(run_dir / "preparation_result.json", prep)
        runtime._complete_phase(run_dir, state, "preparation")
        calibration = {"status": "not_applicable", "matched": True, "reason": "Codex direct retry blueprint has no calibration queries; execution/evaluation supplies evidence."}
        write_json(run_dir / "calibration_result.json", calibration)
        runtime._complete_phase(run_dir, state, "calibration")
        execution = runtime._execute(blueprint, run_dir)
        write_json(run_dir / "execution_bundle.json", execution)
        runtime._complete_phase(run_dir, state, "execution")
        try:
            evaluation_obj = runtime._evaluate(blueprint, execution)
        except Exception as eval_exc:
            evaluation_obj = ReproductionEvaluation(False, False, 0.0, False, f"evaluation failed: {eval_exc}", evidence={"evaluation_exception": traceback.format_exc()})
        evaluation = evaluation_obj.to_dict()
        write_json(run_dir / "evaluation_result.json", evaluation)
        report = {"run_id": run_id, "run_dir": str(run_dir), "status": status_from_eval(evaluation), "evaluation": evaluation, "codex_analysis": analysis}
        write_json(run_dir / "reproduction_report.json", report)
        runtime._complete_phase(run_dir, state, "completed")
        return {"category": category, "slug": slug, "attempt_dir": str(attempt_dir), "run_dir": str(run_dir), "status": report["status"], "reason": evaluation.get("reason", ""), "evaluation": evaluation}
    except Exception as exc:
        failure = {"status": "failed", "error": str(exc), "exception_type": type(exc).__name__, "traceback": traceback.format_exc(), "codex_analysis": analysis, "timestamp": time.time()}
        write_json(run_dir / "failure.json", failure)
        state.status = "failed"
        state.phase = state.phase or "failed"
        state.last_error = str(exc)
        save_state(run_dir, state)
        return {"category": category, "slug": slug, "attempt_dir": str(attempt_dir), "run_dir": str(run_dir), "status": "failed", "reason": str(exc)}


def main() -> int:
    data = json.loads(SUMMARY.read_text())
    partials = [item for item in data["posts"] if item.get("final_status") == "partial"]
    runtime = ReproductionRuntime()
    records = []
    jsonl = ROOT / "attempt_codex_partial_summary.jsonl"
    if jsonl.exists():
        jsonl.unlink()
    for item in partials:
        record = run_one(runtime, item)
        records.append(record)
        with jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps({"slug": record["slug"], "status": record["status"], "run_dir": record["run_dir"]}, ensure_ascii=False))
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    summary = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source_summary": str(SUMMARY), "counts": counts, "records": records}
    write_json(ROOT / "attempt_codex_partial_summary.json", summary)
    lines = ["# Codex Partial Retry Summary", "", f"Generated at: {summary['generated_at']}", "", "## Counts", ""]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines += ["", "## Records", ""]
    for r in records:
        lines.append(f"- `{r['category']}/{r['slug']}`: **{r['status']}** — {r.get('reason','')[:240]}  ")
        lines.append(f"  Run: `{r['run_dir']}`")
    (ROOT / "attempt_codex_partial_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
