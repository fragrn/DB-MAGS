"""Background workload runner and phase metrics collection."""

from __future__ import annotations

import math
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from agent.config import RuntimeConfig, resolve_runtime_path
from agent.probes.mysql_probe import MySQLProbe
from agent.probes.os_probe import OSProbe
from agent.tools import BENCHBASE_BENCHMARKS


DEFAULT_CONFIG_PATHS = {
    "tpcc": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
    "tpch": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpch_1SF_config.xml",
}

DEFAULT_WORKLOAD = {
    "enabled": False,
    "runner": "benchbase",
    "benchmark": "tpcc",
    "database": "",
    "config_path": ".tools/benchbase-main/target/benchbase-mysql/config/mysql/local_tpcc_10W_config.xml",
    "jar_path": ".tools/benchbase-main/target/benchbase-mysql/benchbase.jar",
    "java_bin": "/opt/homebrew/opt/openjdk/bin/java",
    "warmup_sec": 60,
    "baseline_sec": 30,
    "injection_observe_sec": 30,
    "recovery_sec": 30,
    "sample_interval_sec": 5,
    "duration_sec": None,
}


class BenchBaseWorkloadRunner:
    """Run BenchBase as a long-lived background workload process."""

    def __init__(self, config: RuntimeConfig, workload_config: dict[str, Any], round_dir: Path):
        self.config = config
        self.workload_config = normalize_workload_config(workload_config, config.default_database)
        self.round_dir = Path(round_dir).resolve()
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.runtime_config_path: Path | None = None
        self.stdout_path = self.round_dir / "workload_stdout.log"
        self.stderr_path = self.round_dir / "workload_stderr.log"
        self._stdout_handle = None
        self._stderr_handle = None

    def start(self) -> dict[str, Any]:
        self.round_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_config_path = self._materialize_config()
        jar_path = _resolve_path(str(self.workload_config["jar_path"]))
        java_bin = str(self.workload_config.get("java_bin") or "java")
        benchmark = str(self.workload_config.get("benchmark") or "tpcc")
        command = [
            java_bin,
            "-jar",
            str(jar_path),
            "-b",
            benchmark,
            "-c",
            str(self.runtime_config_path),
            "--create=false",
            "--load=false",
            "--execute=true",
        ]
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=str(jar_path.parent),
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            text=True,
        )
        self.started_at = time.time()
        return {
            "event": "start_workload",
            "timestamp": self.started_at,
            "pid": self.process.pid,
            "command": command,
            "runtime_config_path": str(self.runtime_config_path),
        }

    def stop(self, timeout_sec: float = 10.0) -> dict[str, Any]:
        event = {"event": "stop_workload", "timestamp": time.time()}
        if self.process is None:
            event.update({"running_before_stop": False, "exit_code": None})
            self._close_logs()
            return event
        running = self.process.poll() is None
        event["running_before_stop"] = running
        if running:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout_sec)
                event["killed"] = True
        event["exit_code"] = self.process.poll()
        self._close_logs()
        return event

    def status(self) -> dict[str, Any]:
        exit_code = self.process.poll() if self.process else None
        running = bool(self.process and exit_code is None)
        return {
            "pid": self.process.pid if self.process else None,
            "running": running,
            "start_time": self.started_at,
            "elapsed_sec": round(time.time() - self.started_at, 3) if self.started_at else 0.0,
            "exit_code": exit_code,
            "stdout_tail": _tail(self.stdout_path),
            "stderr_tail": _tail(self.stderr_path),
            "runtime_config_path": str(self.runtime_config_path) if self.runtime_config_path else "",
        }

    def _materialize_config(self) -> Path:
        source = _resolve_path(str(self.workload_config["config_path"]))
        if not source.exists():
            raise FileNotFoundError(f"BenchBase workload config not found: {source}")
        tree = ET.parse(source)
        root = tree.getroot()
        duration_sec = int(self.workload_config.get("duration_sec") or _total_needed_duration(self.workload_config))
        terminals = self.workload_config.get("terminals")
        if duration_sec > 0:
            for elem in root.findall(".//work/time"):
                elem.text = str(duration_sec)
        if terminals is not None:
            elem = root.find("terminals")
            if elem is not None:
                elem.text = str(int(terminals))
        out = (self.round_dir / f"runtime_{source.name}").resolve()
        tree.write(out, encoding="utf-8", xml_declaration=True)
        return out

    def _close_logs(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass


class MetricsCollector:
    """Collect phase metrics as time-series and window summaries."""

    def __init__(self, config: RuntimeConfig, database: str, runner: BenchBaseWorkloadRunner | None = None):
        self.config = config
        self.database = database
        self.runner = runner
        self.slow_threshold_sec: float | None = None

    def collect_window(self, phase: str, duration_sec: float, interval_sec: float) -> dict[str, Any]:
        count = max(1, int(math.ceil(float(duration_sec) / max(float(interval_sec), 0.001))))
        samples = []
        for i in range(count):
            sample = self.sample_once(phase, i, interval_sec)
            samples.append(sample)
            status = sample.get("workload_status") or {}
            if self.runner and status.get("running") is False:
                raise RuntimeError(_workload_exit_message(phase, status))
        summary = summarize_window(samples)
        return {
            "phase": phase,
            "duration_sec": duration_sec,
            "sample_interval_sec": interval_sec,
            "sample_count": len(samples),
            "samples": samples,
            "summary": summary,
            "summary_flat": summary.get("flat", {}),
            "db_metrics": summary.get("db_metrics", {}),
            "workload": summary.get("workload", {}),
            "os_metrics": summary.get("os_metrics", {}),
        }

    def sample_once(self, phase: str, index: int, interval_sec: float) -> dict[str, Any]:
        probe = MySQLProbe(
            database=self.database,
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
        )
        db_metrics = probe.db_metrics()
        workload = probe.workload_probe(
            interval_sec=float(interval_sec),
            slow_threshold_sec=self.slow_threshold_sec,
        )
        slow_sql = workload.get("performance_schema_slow_sql") or {}
        if self.slow_threshold_sec is None and slow_sql.get("slow_threshold_sec") is not None:
            self.slow_threshold_sec = float(slow_sql["slow_threshold_sec"])
        return {
            "phase": phase,
            "index": index,
            "timestamp": time.time(),
            "db_metrics": db_metrics,
            "workload": workload,
            "os_metrics": OSProbe().collect(),
            "workload_status": self.runner.status() if self.runner else {},
        }


def normalize_workload_config(workload: dict[str, Any], default_database: str) -> dict[str, Any]:
    raw = workload or {}
    cfg = dict(DEFAULT_WORKLOAD)
    cfg.update(raw)
    cfg["benchmark"] = str(cfg.get("benchmark") or "tpcc").lower()
    if cfg["benchmark"] not in BENCHBASE_BENCHMARKS:
        raise ValueError(f"Unsupported workload benchmark: {cfg['benchmark']}")
    if "config_path" not in raw:
        if cfg["benchmark"] in DEFAULT_CONFIG_PATHS:
            cfg["config_path"] = DEFAULT_CONFIG_PATHS[cfg["benchmark"]]
        elif cfg.get("enabled"):
            raise ValueError(f"workload.config_path is required for benchmark {cfg['benchmark']}")
    if not cfg.get("database"):
        cfg["database"] = default_database
    cfg["jar_path"] = resolve_runtime_path(str(cfg["jar_path"]))
    cfg["config_path"] = resolve_runtime_path(str(cfg["config_path"]))
    java_bin = str(cfg.get("java_bin") or "java")
    java_path = Path(java_bin).expanduser()
    if java_path.is_absolute() or len(java_path.parts) > 1:
        cfg["java_bin"] = resolve_runtime_path(java_path)
    for key in ("warmup_sec", "baseline_sec", "injection_observe_sec", "recovery_sec", "sample_interval_sec"):
        cfg[key] = float(cfg.get(key, DEFAULT_WORKLOAD[key]))
    if cfg.get("duration_sec") is not None:
        cfg["duration_sec"] = float(cfg["duration_sec"])
    if cfg.get("terminals") is not None:
        cfg["terminals"] = int(cfg["terminals"])
    return cfg


def _workload_exit_message(phase: str, status: dict[str, Any]) -> str:
    return (
        f"Background workload exited before Phase 10 during {phase}: "
        f"exit_code={status.get('exit_code')}, "
        f"runtime_config_path={status.get('runtime_config_path', '')}, "
        f"stdout_tail={status.get('stdout_tail', '')!r}, "
        f"stderr_tail={status.get('stderr_tail', '')!r}"
    )


def make_workload_runner(config: RuntimeConfig, workload_config: dict[str, Any], round_dir: Path) -> BenchBaseWorkloadRunner:
    cfg = normalize_workload_config(workload_config, config.default_database)
    if str(cfg.get("runner", "benchbase")).lower() != "benchbase":
        raise ValueError(f"Unsupported workload runner: {cfg.get('runner')}")
    return BenchBaseWorkloadRunner(config, cfg, round_dir)


def make_metrics_collector(
    config: RuntimeConfig,
    workload_config: dict[str, Any],
    runner: BenchBaseWorkloadRunner | None = None,
) -> MetricsCollector:
    cfg = normalize_workload_config(workload_config, config.default_database)
    return MetricsCollector(config, str(cfg["database"]), runner=runner)


def summarize_window(samples: list[dict[str, Any]]) -> dict[str, Any]:
    db_keys = [
        "Threads_connected",
        "Threads_running",
        "Max_used_connections",
        "Slow_queries",
        "Innodb_row_lock_waits",
        "Innodb_row_lock_time",
        "Innodb_row_lock_time_avg",
        "Aborted_connects",
        "Innodb_deadlocks",
        "Innodb_lock_wait_timeouts",
        "Created_tmp_tables",
        "Created_tmp_disk_tables",
        "Sort_merge_passes",
        "Innodb_buffer_pool_reads",
        "Innodb_buffer_pool_read_requests",
        "Innodb_log_waits",
        "Binlog_cache_disk_use",
        "Com_commit",
        "metadata_lock_wait_count",
    ]
    db_summary = {k: _numeric_stats([_metric(s, "db_metrics", k) for s in samples]) for k in db_keys}
    workload_summary = {
        "qps": _numeric_stats([_metric(s, "workload", "qps") for s in samples]),
        "tps": _numeric_stats([_metric(s, "workload", "tps") for s in samples]),
    }
    slow_sql_summary = _summarize_slow_sql_intervals([
        (sample.get("workload") or {}).get("performance_schema_slow_sql") or {}
        for sample in samples
    ])
    query_latency_summary = _summarize_query_latency_intervals([
        (sample.get("workload") or {}).get("performance_schema_query_latency") or {}
        for sample in samples
    ])
    cpu_summary = {
        "usage_ratio": _numeric_stats([
            ((s.get("os_metrics") or {}).get("cpu_usage") or {}).get("usage_ratio")
            for s in samples
        ])
    }
    flat = {
        "Threads_connected": db_summary["Threads_connected"]["avg"],
        "Threads_connected_max": db_summary["Threads_connected"]["max"],
        "Threads_running": db_summary["Threads_running"]["avg"],
        "Threads_running_max": db_summary["Threads_running"]["max"],
        "Max_used_connections": db_summary["Max_used_connections"]["max"],
        "Slow_queries": db_summary["Slow_queries"]["last"],
        "Innodb_row_lock_waits": db_summary["Innodb_row_lock_waits"]["last"],
        "Innodb_row_lock_time": db_summary["Innodb_row_lock_time"]["last"],
        "Innodb_row_lock_time_avg": db_summary["Innodb_row_lock_time_avg"]["avg"],
        "Aborted_connects": db_summary["Aborted_connects"]["last"],
        "Innodb_deadlocks": db_summary["Innodb_deadlocks"]["last"],
        "Innodb_lock_wait_timeouts": db_summary["Innodb_lock_wait_timeouts"]["last"],
        "Created_tmp_tables": db_summary["Created_tmp_tables"]["last"],
        "Created_tmp_disk_tables": db_summary["Created_tmp_disk_tables"]["last"],
        "Sort_merge_passes": db_summary["Sort_merge_passes"]["last"],
        "Innodb_buffer_pool_reads": db_summary["Innodb_buffer_pool_reads"]["last"],
        "Innodb_buffer_pool_read_requests": db_summary["Innodb_buffer_pool_read_requests"]["last"],
        "Innodb_log_waits": db_summary["Innodb_log_waits"]["last"],
        "Binlog_cache_disk_use": db_summary["Binlog_cache_disk_use"]["last"],
        "Com_commit": db_summary["Com_commit"]["last"],
        "metadata_lock_wait_count": db_summary["metadata_lock_wait_count"]["max"],
        "qps": workload_summary["qps"]["avg"],
        "tps": workload_summary["tps"]["avg"],
        "cpu_usage_ratio": cpu_summary["usage_ratio"]["avg"],
    }
    return {
        "db_metrics": db_summary,
        "workload": workload_summary,
        "performance_schema_slow_sql": slow_sql_summary,
        "query_latency_top10": query_latency_summary.get("top10", []),
        "query_latency_overall": query_latency_summary.get("overall", {}),
        "os_metrics": {"cpu_usage": cpu_summary},
        "flat": flat,
    }


def make_evaluation_pair(baseline_window: dict[str, Any], injection_window: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = dict(baseline_window.get("summary_flat", {}))
    after = dict(injection_window.get("summary_flat", {}))
    base_summary = baseline_window.get("summary", {})
    inj_summary = injection_window.get("summary", {})

    base_db = base_summary.get("db_metrics", {})
    inj_db = inj_summary.get("db_metrics", {})
    base_wl = base_summary.get("workload", {})
    inj_wl = inj_summary.get("workload", {})

    base_qps = float((base_wl.get("qps") or {}).get("avg") or 0.0)
    inj_qps = float((inj_wl.get("qps") or {}).get("avg") or 0.0)
    base_tps = float((base_wl.get("tps") or {}).get("avg") or 0.0)
    inj_tps = float((inj_wl.get("tps") or {}).get("avg") or 0.0)
    after["qps_ratio"] = (inj_qps / base_qps) if base_qps > 0 else 1.0
    after["tps_ratio"] = (inj_tps / base_tps) if base_tps > 0 else 1.0
    baseline["qps_ratio"] = 1.0
    baseline["tps_ratio"] = 1.0

    after["slow_query_count_delta"] = _delta(inj_db, base_db, "Slow_queries")
    after["lock_wait_time_delta"] = _delta(inj_db, base_db, "Innodb_row_lock_time")
    after["deadlock_delta"] = _delta(inj_db, base_db, "Innodb_deadlocks")
    after["connection_error_delta"] = _delta(inj_db, base_db, "Aborted_connects")
    after["network_error_delta"] = after["connection_error_delta"]
    after["Created_tmp_tables_delta"] = _delta(inj_db, base_db, "Created_tmp_tables")
    after["Created_tmp_disk_tables_delta"] = _delta(inj_db, base_db, "Created_tmp_disk_tables")
    after["Sort_merge_passes_delta"] = _delta(inj_db, base_db, "Sort_merge_passes")
    after["Innodb_buffer_pool_reads_delta"] = _delta(inj_db, base_db, "Innodb_buffer_pool_reads")
    after["Innodb_log_waits_delta"] = _delta(inj_db, base_db, "Innodb_log_waits")
    after["Binlog_cache_disk_use_delta"] = _delta(inj_db, base_db, "Binlog_cache_disk_use")
    after["metadata_lock_wait_count"] = float((inj_db.get("metadata_lock_wait_count") or {}).get("max") or 0.0)
    after["metadata_lock_evidence"] = _metadata_lock_evidence(injection_window)
    after["active_sessions_delta"] = max(
        0.0,
        float((inj_db.get("Threads_connected") or {}).get("max") or 0.0)
        - float((base_db.get("Threads_connected") or {}).get("avg") or 0.0),
    )
    baseline["slow_query_count_delta"] = 0.0
    baseline["lock_wait_time_delta"] = 0.0
    baseline["deadlock_delta"] = 0.0
    baseline["connection_error_delta"] = 0.0
    baseline["network_error_delta"] = 0.0
    baseline["Created_tmp_tables_delta"] = 0.0
    baseline["Created_tmp_disk_tables_delta"] = 0.0
    baseline["Sort_merge_passes_delta"] = 0.0
    baseline["Innodb_buffer_pool_reads_delta"] = 0.0
    baseline["Innodb_log_waits_delta"] = 0.0
    baseline["Binlog_cache_disk_use_delta"] = 0.0
    baseline["metadata_lock_wait_count"] = float((base_db.get("metadata_lock_wait_count") or {}).get("max") or 0.0)
    baseline["metadata_lock_evidence"] = _metadata_lock_evidence(baseline_window)
    baseline["active_sessions_delta"] = 0.0

    # Evaluate lock waits as a rate/delta rather than cumulative global counter.
    base_lock_delta = max(1.0, _self_delta(base_db, "Innodb_row_lock_waits"))
    inj_lock_delta = max(0.0, _self_delta(inj_db, "Innodb_row_lock_waits"))
    baseline["Innodb_row_lock_waits"] = base_lock_delta
    after["Innodb_row_lock_waits"] = inj_lock_delta
    baseline["Threads_connected"] = float((base_db.get("Threads_connected") or {}).get("avg") or 0.0)
    after["Threads_connected"] = float((inj_db.get("Threads_connected") or {}).get("max") or 0.0)
    baseline["Threads_running"] = float((base_db.get("Threads_running") or {}).get("avg") or 0.0)
    after["Threads_running"] = float((inj_db.get("Threads_running") or {}).get("max") or 0.0)

    baseline["workload"] = {"qps": base_qps, "tps": base_tps}
    after["workload"] = {"qps": inj_qps, "tps": inj_tps}
    baseline["write_latency_ratio"] = 1.0
    after["write_latency_ratio"] = _latency_ratio(
        base_summary.get("query_latency_overall") or {},
        inj_summary.get("query_latency_overall") or {},
    )
    baseline["buffer_pool_read_ratio"] = 1.0
    after["buffer_pool_read_ratio"] = _buffer_pool_read_ratio(base_db, inj_db)
    baseline["performance_schema_slow_sql"] = dict(
        base_summary.get("performance_schema_slow_sql") or {}
    )
    after["performance_schema_slow_sql"] = dict(
        inj_summary.get("performance_schema_slow_sql") or {}
    )
    baseline["query_latency_top10"] = list(base_summary.get("query_latency_top10") or [])
    baseline["query_latency_overall"] = dict(base_summary.get("query_latency_overall") or {})
    after["query_latency_top10"] = list(inj_summary.get("query_latency_top10") or [])
    after["query_latency_overall"] = dict(inj_summary.get("query_latency_overall") or {})
    return baseline, after


def _summarize_slow_sql_intervals(intervals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate exact interval deltas into one phase-level slow SQL ratio."""
    if not intervals:
        return {"available": False, "error": "no performance_schema histogram samples"}
    unavailable = [item for item in intervals if not item.get("available")]
    if unavailable:
        return {
            "available": False,
            "schema": next((item.get("schema") for item in intervals if item.get("schema")), None),
            "slow_threshold_sec": next(
                (item.get("slow_threshold_sec") for item in intervals if item.get("slow_threshold_sec") is not None),
                None,
            ),
            "reset_detected": any(bool(item.get("reset_detected")) for item in intervals),
            "error": "; ".join(
                str(item.get("error") or "histogram sample unavailable") for item in unavailable
            ),
        }

    total_count = sum(int(item.get("total_statement_count") or 0) for item in intervals)
    slow_count = sum(int(item.get("slow_statement_count") or 0) for item in intervals)
    return {
        "available": True,
        "schema": intervals[0].get("schema"),
        "slow_threshold_sec": intervals[0].get("slow_threshold_sec"),
        "effective_threshold_sec": next(
            (item.get("effective_threshold_sec") for item in intervals if item.get("effective_threshold_sec") is not None),
            None,
        ),
        "total_statement_count": total_count,
        "slow_statement_count": slow_count,
        "slow_ratio": (slow_count / total_count) if total_count > 0 else 0.0,
        "sample_count": len(intervals),
        "reset_detected": False,
    }


def _summarize_query_latency_intervals(intervals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-sample digest latency diffs into one phase summary."""
    if not intervals:
        return {
            "available": False,
            "error": "no performance_schema digest latency samples",
            "top10": [],
            "overall": {
                "available": False,
                "error": "no performance_schema digest latency samples",
            },
        }
    unavailable = [item for item in intervals if not item.get("available")]
    available = [item for item in intervals if item.get("available")]
    if not available:
        return {
            "available": False,
            "schema": next((item.get("schema") for item in intervals if item.get("schema")), None),
            "error": "; ".join(
                str(item.get("error") or "digest latency sample unavailable") for item in unavailable
            ) or "performance_schema digest latency unavailable",
            "top10": [],
            "overall": {
                "available": False,
                "error": "; ".join(
                    str(item.get("error") or "digest latency sample unavailable") for item in unavailable
                ) or "performance_schema digest latency unavailable",
            },
        }

    by_digest: dict[str, dict[str, Any]] = {}
    total_count = 0
    total_latency_ms = 0.0
    medians: list[float] = []
    p95s: list[float] = []
    max_latency_ms = 0.0

    for item in available:
        overall = item.get("overall") or {}
        count = int(overall.get("count") or 0)
        total = float(overall.get("total_latency_ms") or 0.0)
        total_count += count
        total_latency_ms += total
        if overall.get("median_latency_ms") is not None:
            medians.append(float(overall["median_latency_ms"]))
        if overall.get("p95_latency_ms") is not None:
            p95s.append(float(overall["p95_latency_ms"]))
        max_latency_ms = max(max_latency_ms, float(overall.get("max_latency_ms") or 0.0))
        for row in item.get("top10") or []:
            digest = str(row.get("digest") or row.get("digest_text") or "")
            if not digest:
                continue
            target = by_digest.setdefault(digest, {
                "digest": row.get("digest"),
                "digest_text": row.get("digest_text") or "",
                "execution_count": 0,
                "total_latency_ms": 0.0,
                "avg_latency_ms": 0.0,
                "median_latency_ms": None,
                "p95_latency_ms": None,
                "max_latency_ms": 0.0,
                "max_latency_source": row.get("max_latency_source"),
                "sample_count": 0,
            })
            row_count = int(row.get("execution_count") or 0)
            row_total = float(row.get("total_latency_ms") or 0.0)
            target["execution_count"] += row_count
            target["total_latency_ms"] += row_total
            target["max_latency_ms"] = max(
                float(target.get("max_latency_ms") or 0.0),
                float(row.get("max_latency_ms") or 0.0),
            )
            if row.get("median_latency_ms") is not None:
                target["median_latency_ms"] = max(
                    float(target["median_latency_ms"] or 0.0),
                    float(row["median_latency_ms"]),
                )
            if row.get("p95_latency_ms") is not None:
                target["p95_latency_ms"] = max(
                    float(target["p95_latency_ms"] or 0.0),
                    float(row["p95_latency_ms"]),
                )
            target["sample_count"] += 1

    rows = []
    for row in by_digest.values():
        count = int(row.get("execution_count") or 0)
        total = float(row.get("total_latency_ms") or 0.0)
        row["avg_latency_ms"] = round(total / count, 3) if count else 0.0
        row["total_latency_ms"] = round(total, 3)
        rows.append(row)
    rows.sort(
        key=lambda item: float(
            item.get("max_latency_ms")
            or item.get("p95_latency_ms")
            or item.get("avg_latency_ms")
            or 0.0
        ),
        reverse=True,
    )
    return {
        "available": not unavailable,
        "partial": bool(unavailable),
        "schema": next((item.get("schema") for item in available if item.get("schema")), None),
        "sample_count": len(available),
        "unavailable_sample_count": len(unavailable),
        "error": "; ".join(str(item.get("error")) for item in unavailable if item.get("error")),
        "top10": rows[:10],
        "overall": {
            "available": bool(available),
            "count": total_count,
            "avg_latency_ms": round(total_latency_ms / total_count, 3) if total_count else 0.0,
            "median_latency_ms": max(medians) if medians else None,
            "p95_latency_ms": max(p95s) if p95s else None,
            "max_latency_ms": round(max_latency_ms, 3),
            "total_latency_ms": round(total_latency_ms, 3),
            "sample_count": len(available),
        },
    }


def _metric(sample: dict[str, Any], section: str, key: str) -> Any:
    return (sample.get(section) or {}).get(key)


def _numeric_stats(values: list[Any]) -> dict[str, float]:
    nums = []
    for value in values:
        try:
            nums.append(float(value))
        except (TypeError, ValueError):
            pass
    if not nums:
        return {"first": 0.0, "last": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0, "delta": 0.0}
    return {
        "first": nums[0],
        "last": nums[-1],
        "min": min(nums),
        "max": max(nums),
        "avg": sum(nums) / len(nums),
        "delta": nums[-1] - nums[0],
    }


def _delta(after_summary: dict[str, Any], baseline_summary: dict[str, Any], key: str) -> float:
    after_last = float((after_summary.get(key) or {}).get("last") or 0.0)
    baseline_last = float((baseline_summary.get(key) or {}).get("last") or 0.0)
    return max(0.0, after_last - baseline_last)


def _self_delta(summary: dict[str, Any], key: str) -> float:
    return max(0.0, float((summary.get(key) or {}).get("delta") or 0.0))


def _metadata_lock_evidence(window: dict[str, Any]) -> str:
    for sample in window.get("samples", []) or []:
        metrics = sample.get("db_metrics") or {}
        evidence = str(metrics.get("metadata_lock_evidence") or "")
        if evidence:
            return evidence
        processlist = metrics.get("processlist") or []
        for row in processlist:
            state = str(row.get("State") or "")
            if "metadata lock" in state.lower():
                return str(row.get("Info") or state)[:200]
    return ""


def _latency_ratio(base_overall: dict[str, Any], inj_overall: dict[str, Any]) -> float:
    base = float(base_overall.get("avg_latency_ms") or 0.0)
    inj = float(inj_overall.get("avg_latency_ms") or 0.0)
    if base <= 0:
        return 1.0 if inj <= 0 else float("inf")
    return inj / base


def _buffer_pool_read_ratio(base_db: dict[str, Any], inj_db: dict[str, Any]) -> float:
    base_reads = max(0.0, _self_delta(base_db, "Innodb_buffer_pool_reads"))
    base_requests = max(1.0, _self_delta(base_db, "Innodb_buffer_pool_read_requests"))
    inj_reads = max(0.0, _self_delta(inj_db, "Innodb_buffer_pool_reads"))
    inj_requests = max(1.0, _self_delta(inj_db, "Innodb_buffer_pool_read_requests"))
    base_ratio = base_reads / base_requests
    inj_ratio = inj_reads / inj_requests
    if base_ratio <= 0:
        return 1.0 if inj_ratio <= 0 else float("inf")
    return inj_ratio / base_ratio


def _total_needed_duration(cfg: dict[str, Any]) -> int:
    return int(
        float(cfg.get("warmup_sec", 0))
        + float(cfg.get("baseline_sec", 0))
        + float(cfg.get("injection_observe_sec", 0))
        + float(cfg.get("recovery_sec", 0))
        + 30
    )


def _resolve_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path.cwd() / p


def _tail(path: Path, max_chars: int = 2000) -> str:
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except Exception as exc:
        return f"<tail error: {exc}>"
