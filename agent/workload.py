"""Background workload runner and phase metrics collection."""

from __future__ import annotations

import math
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from agent.config import RuntimeConfig
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
    "injection_observe_sec": 60,
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
        workload = probe.workload_probe(interval_sec=float(interval_sec))
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
    ]
    db_summary = {k: _numeric_stats([_metric(s, "db_metrics", k) for s in samples]) for k in db_keys}
    workload_summary = {
        "qps": _numeric_stats([_metric(s, "workload", "qps") for s in samples]),
        "tps": _numeric_stats([_metric(s, "workload", "tps") for s in samples]),
    }
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
        "qps": workload_summary["qps"]["avg"],
        "tps": workload_summary["tps"]["avg"],
        "cpu_usage_ratio": cpu_summary["usage_ratio"]["avg"],
    }
    return {
        "db_metrics": db_summary,
        "workload": workload_summary,
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
    after["active_sessions_delta"] = max(
        0.0,
        float((inj_db.get("Threads_connected") or {}).get("max") or 0.0)
        - float((base_db.get("Threads_connected") or {}).get("avg") or 0.0),
    )
    baseline["slow_query_count_delta"] = 0.0
    baseline["lock_wait_time_delta"] = 0.0
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
    return baseline, after


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
