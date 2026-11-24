"""Real M1-M7 regression matrix: TPCC background plus anomaly injections.

These tests are intentionally opt-in because TPCC writes to the configured
database and some ChaosBlade cases affect the host. Run with ``-s`` to see the
case progress and metric summaries in the terminal.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from db_repro_agent.evaluation.incident_evaluator import IncidentEvaluator
from db_repro_agent.execution.dispatcher import ActionDispatcher
from db_repro_agent.graph.evidence_registry import EvidenceRuleRegistry
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.mysql_metrics import MySQLMetricsProvider
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.slow_log import SlowLogTracker
from db_repro_agent.metrics.system_metrics import SystemMetricsProvider
from db_repro_agent.metrics.timeline import ExperimentPhase
from db_repro_agent.models.action import (
    ActionBase,
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
    TransactionActor,
    TransactionStep,
)
from db_repro_agent.models.common import ResultStatus
from db_repro_agent.reproduction.direct_runner import DirectReproductionRequest, DirectRunner
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RUN_ENV = "DBMAGS_M1_7_RUN_REAL"
RISKY_RUN_ENV = "DBMAGS_M1_7_INCLUDE_RISKY"


@dataclass(frozen=True)
class Case:
    name: str
    oracle_rule_id: str
    action_factory: Callable[[str, str], ActionBase]
    expected_hit: bool = False
    risky: bool = False
    requires_slow_log: bool = False


class PhaseAwareSlowLogProvider:
    """Expose the slow-log delta under M6's ``new_slow_log_entries`` name."""

    def __init__(self, tracker: SlowLogTracker) -> None:
        self.tracker = tracker
        self.marker = None

    def set_phase(self, phase: ExperimentPhase) -> None:
        if phase == ExperimentPhase.INJECTION and self.marker is None:
            self.marker = self.tracker.mark()

    def collect(self) -> dict[str, float]:
        if self.marker is None:
            return {"new_slow_log_entries": 0.0}
        return {"new_slow_log_entries": self.tracker.collect(self.marker)["slow_queries_delta"]}


class RealMetricsCollector(MetricsCollector):
    def __init__(self, providers, slow_log_provider: PhaseAwareSlowLogProvider) -> None:
        super().__init__(providers)
        self.slow_log_provider = slow_log_provider

    def collect_once(self, timeline, *, phase=None):
        self.slow_log_provider.set_phase(phase or timeline.current_phase)
        return super().collect_once(timeline, phase=phase)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _mysql_config(dotenv: dict[str, str]) -> MySQLConfig:
    return MySQLConfig(
        host=dotenv.get("DBMAGS_MYSQL_HOST", dotenv.get("DBMAGS_SERVER_ADDRESS", "127.0.0.1")),
        port=int(dotenv.get("DBMAGS_MYSQL_PORT", "3306")),
        user=dotenv.get("DBMAGS_MYSQL_USER", dotenv.get("DBMAGS_SERVER_USERNAME", "root")),
        password=dotenv.get("DBMAGS_MYSQL_PASSWORD", dotenv.get("DBMAGS_SERVER_PASSWORD", "")),
        database=dotenv.get("DBMAGS_MYSQL_DB", dotenv.get("DBMAGS_DEFAULT_DATABASE", "tpcc10_test")),
        # MySQLAdapter uses this value for connect/read/write timeouts. Keep it
        # above the longest SQL Action timeout used by this test matrix.
        connect_timeout=30,
    )


def _tpcc_config(tmp_path: Path, config: MySQLConfig) -> Path:
    source = REPO_ROOT / ".tools/benchbase-main/target/benchbase-mysql/config/mysql/sample_tpcc_config.xml"
    text = source.read_text(encoding="utf-8")
    url = html.escape(
        f"jdbc:mysql://{config.host}:{config.port}/{config.database}?"
        "rewriteBatchedStatements=true&allowPublicKeyRetrieval=true&sslMode=DISABLED"
    )
    text = re.sub(r"<url>.*?</url>", f"<url>{url}</url>", text, count=1, flags=re.DOTALL)
    text = re.sub(
        r"<username>.*?</username>",
        f"<username>{html.escape(config.user)}</username>",
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"<password>.*?</password>",
        f"<password>{html.escape(config.password)}</password>",
        text,
        count=1,
        flags=re.DOTALL,
    )
    destination = tmp_path / "tpcc_background.xml"
    destination.write_text(text, encoding="utf-8")
    return destination


def _sql_sleep(_: str, database: str) -> SQLAction:
    return SQLAction(
        action_id="sql-concurrent-sleep",
        target_node="connections_up",
        database=database,
        sql="SELECT SLEEP(5)",
        concurrency=3,
        execution_mode="concurrent",
        duration_sec=5,
        timeout_sec=15,
    )


def _row_lock(_: str, database: str) -> TransactionAction:
    return TransactionAction(
        action_id="warehouse-row-lock",
        target_node="row_lock",
        database=database,
        actors=[
            TransactionActor(
                actor_id="holder",
                role="holder",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(
                        sql="SELECT w_id FROM warehouse WHERE w_id=1 FOR UPDATE",
                        delay_after_sec=10,
                    ),
                    TransactionStep(sql="ROLLBACK"),
                ],
            ),
            TransactionActor(
                actor_id="waiter",
                role="waiter",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(sql="SELECT w_id FROM warehouse WHERE w_id=1 FOR UPDATE"),
                    TransactionStep(sql="ROLLBACK"),
                ],
            ),
        ],
        duration_sec=10,
        timeout_sec=15,
    )


def _slow_query(_: str, database: str) -> SQLAction:
    return SQLAction(
        action_id="slow-query-sleep",
        target_node="slow_query",
        database=database,
        sql="SELECT SLEEP(12)",
        duration_sec=12,
        timeout_sec=20,
    )


def _full_scan(_: str, database: str) -> SQLAction:
    return SQLAction(
        action_id="non-indexed-scan",
        target_node="qps_drop",
        database=database,
        sql="SELECT COUNT(*) FROM stock WHERE s_data LIKE '%DBMAGS-NOT-FOUND%'",
        concurrency=3,
        execution_mode="concurrent",
        duration_sec=5,
        timeout_sec=20,
    )


def _sql_cpu(_: str, database: str) -> SQLAction:
    return SQLAction(
        action_id="sql-cpu-benchmark",
        target_node="qps_drop",
        database=database,
        sql="SELECT BENCHMARK(5000000, SHA2('dbmags', 256))",
        concurrency=3,
        execution_mode="concurrent",
        duration_sec=10,
        timeout_sec=30,
    )


def _chaos(resource: str, command: str, blade_path: str, database: str) -> ChaosBladeAction:
    target_node = "cpu_saturation" if resource == "cpu" else "memory_pressure"
    return ChaosBladeAction(
        action_id=f"chaos-{resource}-{abs(hash(command))}",
        target_node=target_node,
        resource=resource,
        command=command,
        duration_sec=20,
        blade_path=blade_path,
    )


def _cases(blade_path: str) -> list[Case]:
    return [
        Case("concurrent_sleep_connections", "connections_up", _sql_sleep, expected_hit=True),
        Case("warehouse_row_lock", "row_lock", _row_lock, expected_hit=True),
        Case("slow_query_log", "slow_query", _slow_query, expected_hit=True, requires_slow_log=True),
        Case("non_indexed_scan", "query_duration", _full_scan, expected_hit=True),
        Case("sql_cpu_benchmark", "sql_cpu_pressure", _sql_cpu, expected_hit=True),
        Case(
            "chaos_cpu_50",
            "cpu_pressure_50",
            lambda node, db: _chaos("cpu", "cpu load --cpu-percent 50 --timeout 20", blade_path, db),
            expected_hit=True,
        ),
        Case(
            "chaos_cpu_80",
            "cpu_pressure_80",
            lambda node, db: _chaos("cpu", "cpu load --cpu-percent 80 --timeout 20", blade_path, db),
            expected_hit=True,
        ),
        # Disabled for local execution: network and disk fault injection can
        # affect the developer workstation and is reserved for an isolated host.
        # Case(
        #     "chaos_network_delay",
        #     "qps_drop",
        #     lambda node, db: _chaos("network", "network delay --time 100 --offset 20", blade_path, db),
        #     risky=True,
        # ),
        # Case(
        #     "chaos_network_loss",
        #     "qps_drop",
        #     lambda node, db: _chaos("network", "network loss --percent 10", blade_path, db),
        #     risky=True,
        # ),
        # Case(
        #     "chaos_disk_load",
        #     "qps_drop",
        #     lambda node, db: _chaos("disk", "disk load --path /tmp --size 100M --timeout 20", blade_path, db),
        #     risky=True,
        # ),
        Case(
            "chaos_memory_load",
            "memory_pressure",
            lambda node, db: _chaos("memory", "mem load --mode ram --mem-percent 30 --timeout 20", blade_path, db),
            expected_hit=True,
            risky=True,
        ),
    ]


def _log(event: str, **fields) -> None:
    payload = " ".join(f"{key}={json.dumps(value, ensure_ascii=False, default=str)}" for key, value in fields.items())
    print(f"[M1-M7][{time.strftime('%H:%M:%S')}] {event} {payload}", flush=True)


def _log_timeline(case: Case, result) -> None:
    _log(
        "case_result",
        case=case.name,
        status=result.status.value,
        action_success=result.action_result.success if result.action_result else None,
        action_error_code=result.action_result.error_code if result.action_result else None,
        action_error_message=result.action_result.error_message if result.action_result else None,
        background_success=result.background_workload_result.success if result.background_workload_result else None,
        state=result.state.phase.value,
    )
    if result.action_result is not None and (
        result.action_result.action_type == "chaosblade" or not result.action_result.success
    ):
        _log(
            "action_diagnostics",
            case=case.name,
            action_type=result.action_result.action_type,
            exit_code=result.action_result.exit_code,
            error_code=result.action_result.error_code,
            error_message=result.action_result.error_message,
            stdout=result.action_result.stdout,
            stderr=result.action_result.stderr,
            metadata=result.action_result.metadata,
        )
    if result.evaluation is not None:
        evidence = result.evaluation.evidence
        _log(
            "evidence",
            rule=result.evaluation.oracle_rule_id,
            hit=evidence.hit,
            status=evidence.status.value,
            aggregate=evidence.aggregate_value,
            reference=evidence.reference_value,
            comparison=evidence.comparison_value,
            reason=evidence.reason,
        )
        if result.evaluation.severity_evidence is not None:
            severity = result.evaluation.severity_evidence
            _log(
                "severity_evidence",
                rule=severity.rule_id,
                hit=severity.hit,
                status=severity.status.value,
                delta=severity.comparison_value,
                baseline=severity.reference_value,
                threshold=1000.0 if severity.rule_id == "row_lock_severe" else None,
                reason=severity.reason,
            )
        if result.evaluation.supporting_evidence:
            _log(
                "supporting_evidence",
                rules=[
                    {
                        "rule": item.rule_id,
                        "hit": item.hit,
                        "status": item.status.value,
                        "value": item.aggregate_value,
                        "reason": item.reason,
                    }
                    for item in result.evaluation.supporting_evidence
                ],
            )
    if result.pre_snapshot is not None and result.post_snapshot is not None:
        pre = result.pre_snapshot.metrics
        post = result.post_snapshot.metrics

        def snapshot_metric(metrics: dict[str, float], name: str) -> float:
            if name in metrics:
                return metrics[name]
            return next(
                (value for metric_name, value in metrics.items() if metric_name.endswith(f".{name}")),
                0.0,
            )

        pre_lock_waits = snapshot_metric(pre, "lock_waits")
        post_lock_waits = snapshot_metric(post, "lock_waits")
        pre_lock_wait_time = snapshot_metric(pre, "lock_wait_time_ms")
        post_lock_wait_time = snapshot_metric(post, "lock_wait_time_ms")
        _log(
            "snapshot_delta",
            pre_lock_waits=pre_lock_waits,
            post_lock_waits=post_lock_waits,
            new_lock_waits=post_lock_waits - pre_lock_waits,
            pre_lock_wait_time_ms=pre_lock_wait_time,
            post_lock_wait_time_ms=post_lock_wait_time,
            new_lock_wait_time_ms=post_lock_wait_time - pre_lock_wait_time,
            severe=(post_lock_wait_time - pre_lock_wait_time) >= 1000,
            max_current_waits=max(
                (
                    sample.value
                    for sample in result.timeline.samples
                    if sample.metric_name == "lock_waits_current" or sample.metric_name.endswith(".lock_waits_current")
                ),
                default=0.0,
            ),
        )
    _log(
        "cleanup",
        case=case.name,
        benchbase_process=result.state.step_results.get("cleaning_up", {}).get("background_workload_cleaned"),
    )


@pytest.mark.integration
@pytest.mark.parametrize("case", _cases(".tools/chaosblade-1.8.0-darwin_arm64/blade"), ids=lambda case: case.name)
def test_real_tpcc_background_anomaly_matrix(case: Case, tmp_path: Path) -> None:
    """Run one real anomaly case with TPCC always acting as background load."""
    if os.environ.get(REAL_RUN_ENV) != "1":
        pytest.skip(f"set {REAL_RUN_ENV}=1 to run real TPCC integration cases")
    if case.risky and os.environ.get(RISKY_RUN_ENV) != "1":
        pytest.skip(f"set {RISKY_RUN_ENV}=1 to run risky {case.name}")

    dotenv = _read_dotenv(REPO_ROOT / ".env")
    config = _mysql_config(dotenv)
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"local MySQL is not reachable: {exc}")

    jar = REPO_ROOT / ".tools/benchbase-main/target/benchbase-mysql/benchbase.jar"
    if not jar.is_file():
        pytest.skip(f"BenchBase jar does not exist: {jar}")
    blade_path = dotenv.get("DBMAGS_CHAOSBLADE_PATH", ".tools/chaosblade-1.8.0-darwin_arm64/blade")
    blade = (REPO_ROOT / blade_path).resolve() if not Path(blade_path).is_absolute() else Path(blade_path)
    if case.name.startswith("chaos_") and not blade.is_file():
        pytest.skip(f"ChaosBlade binary does not exist: {blade}")

    adapter = MySQLAdapter(config)
    slow_log_restore: tuple[str, str] | None = None
    if case.requires_slow_log:
        slow_log_result = adapter.execute_readonly("SHOW VARIABLES LIKE 'slow_query_log'")
        long_query_result = adapter.execute_readonly("SHOW VARIABLES LIKE 'long_query_time'")
        if not slow_log_result.success:
            pytest.fail(slow_log_result.error_message or "unable to inspect slow_query_log")
        if not long_query_result.success:
            pytest.fail(long_query_result.error_message or "unable to inspect long_query_time")
        slow_log_value = str(slow_log_result.data[0]["Value"]) if slow_log_result.data else ""
        long_query_value = str(long_query_result.data[0]["Value"]) if long_query_result.data else "10"
        _log("slow_log_config", slow_query_log=slow_log_value, long_query_time=long_query_value)
        slow_log_restore = (slow_log_value, long_query_value)
        try:
            with adapter.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET GLOBAL slow_query_log = ON")
                    cursor.execute("SET GLOBAL long_query_time = 1")
        except Exception as exc:
            try:
                with adapter.connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET GLOBAL slow_query_log = {'ON' if slow_log_value.upper() == 'ON' else 'OFF'}")
                        cursor.execute("SET GLOBAL long_query_time = %s", (float(long_query_value),))
            except Exception as restore_exc:
                _log("slow_log_partial_restore_failed", error=str(restore_exc))
            pytest.fail(f"slow-query environment configuration failed: {exc}")
        _log("slow_log_configured", slow_query_log="ON", long_query_time=1)

    tpcc_config = _tpcc_config(tmp_path, config)
    results_dir = tmp_path / "benchbase-results"
    background = BenchBaseAction(
        action_id=f"tpcc-background-{case.name}",
        target_node="traffic_surge",
        benchmark="tpcc",
        database=config.database,
        terminals=1,
        rate=10,
        duration_sec=60,
        config_path=str(tpcc_config),
        jar_path=str(jar.resolve()),
        results_dir=str(results_dir),
    )
    slow_log_provider = PhaseAwareSlowLogProvider(SlowLogTracker(adapter))
    collector = RealMetricsCollector(
        {
            "mysql": MySQLMetricsProvider(adapter),
            "system": SystemMetricsProvider(),
            "slow_log": slow_log_provider,
        },
        slow_log_provider,
    )
    runner = DirectRunner(
        dispatcher=ActionDispatcher(adapter),
        sampler=MetricsSampler(collector, interval_seconds=1.0),
        evaluator=IncidentEvaluator(EvidenceRuleRegistry.from_yaml(str(REPO_ROOT / "config/evidence_rules.yaml"))),
    )
    action = case.action_factory(str(case.name), config.database)
    request = DirectReproductionRequest(
        incident_id=f"real-m1-7-{case.name}",
        action=action,
        background_workload=background,
        oracle_rule_id=case.oracle_rule_id,
        baseline_seconds=5.0,
        observe_seconds=5.0,
        recovery_seconds=1.0,
        severity_rule_id="row_lock_severe" if case.name == "warehouse_row_lock" else None,
        supporting_rule_ids=(
            ["active_lock_wait"]
            if case.name == "warehouse_row_lock"
            else ["query_duration"]
            if case.name == "sql_cpu_benchmark"
            else []
        ),
    )

    try:
        _log("case_start", case=case.name, action=action.model_dump(mode="json"), oracle=case.oracle_rule_id)
        result = runner.run(request)
        _log_timeline(case, result)

        assert result.action_result is not None, "M7 did not return an action result"
        assert result.action_result.success is True, result.action_result.error_message
        assert result.background_workload_result is not None
        assert result.background_workload_result.success is True
        assert result.evaluation is not None
        assert result.state.phase.value in {"success", "cleaning_up"}
        cleanup_result = result.state.step_results.get("cleaning_up", {})
        assert cleanup_result.get("cleaned") is True, "M7 cleanup did not complete successfully"
        if case.expected_hit:
            assert result.evaluation.hit is True, result.evaluation.reason
        if case.name == "sql_cpu_benchmark":
            duration_evidence = next(
                (item for item in result.evaluation.supporting_evidence if item.rule_id == "query_duration"),
                None,
            )
            assert duration_evidence is not None and duration_evidence.hit is True, (
                duration_evidence.reason if duration_evidence is not None else "query_duration evidence missing"
            )
    finally:
        if slow_log_restore is not None:
            original_slow_log, original_long_query = slow_log_restore
            try:
                with adapter.connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET GLOBAL slow_query_log = {'ON' if original_slow_log.upper() == 'ON' else 'OFF'}")
                        cursor.execute("SET GLOBAL long_query_time = %s", (float(original_long_query),))
                _log("slow_log_restored", slow_query_log=original_slow_log, long_query_time=original_long_query)
            except Exception as exc:
                pytest.fail(f"slow-query environment restoration failed: {exc}")
