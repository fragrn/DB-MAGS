"""M7 Direct Reproduction tests and acceptance checks."""

from __future__ import annotations

import html
import os
import re
import socket
import time
from pathlib import Path

from db_repro_agent.evaluation.incident_evaluator import IncidentEvaluator
from db_repro_agent.execution.dispatcher import ActionDispatcher
from db_repro_agent.execution.command_executor import _iso
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.mysql_metrics import MySQLMetricsProvider
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.timeline import ExperimentPhase
from db_repro_agent.graph.evidence_registry import EvidenceRuleRegistry
from db_repro_agent.models.action import ActionResult, BenchBaseAction, SQLAction
from db_repro_agent.models.common import ResultStatus
from db_repro_agent.reproduction.direct_runner import DirectReproductionRequest, DirectRunner
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig

import pytest


class FakeProvider:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)
        self.last = 0.0

    def collect(self) -> dict[str, float]:
        try:
            self.last = next(self.values)
        except StopIteration:
            pass
        return {"qps": self.last}


class PhaseAwareProvider:
    def __init__(self, observed_value: float) -> None:
        self.phase = ExperimentPhase.WARMUP
        self.observed_value = observed_value

    def collect(self) -> dict[str, float]:
        value = 100.0 if self.phase in {ExperimentPhase.WARMUP, ExperimentPhase.BASELINE} else self.observed_value
        return {"qps": value}


class PhaseAwareCollector(MetricsCollector):
    def __init__(self, provider: PhaseAwareProvider) -> None:
        super().__init__({"synthetic": provider})
        self.provider = provider

    def collect_once(self, timeline, *, phase=None):
        self.provider.phase = phase or timeline.current_phase
        return super().collect_once(timeline, phase=phase)


class FakeDispatcher:
    def __init__(self, result: ActionResult) -> None:
        self.result = result
        self.calls: list[str] = []
        self.events: list[str] = []
        self.benchbase = FakeBenchBaseExecutor(self.events)

    def validate(self, action):
        self.calls.append("validate")
        self.events.append("action.validate")

    def start(self, action):
        self.calls.append("start")
        self.events.append("action.start")
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="started",
            started_at=_iso(time.time()),
            finished_at=_iso(time.time()),
            duration_ms=0,
        )

    def execute(self, action):
        self.calls.append("execute")
        self.events.append("action.execute")
        return self.result

    def cleanup(self, action):
        self.calls.append("cleanup")
        self.events.append("action.cleanup")
        return self.result.model_copy(update={"success": True, "status": "cleaned"})


class FakeBenchBaseExecutor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[str] = []

    def start(self, action: BenchBaseAction) -> ActionResult:
        self.calls.append("start")
        self.events.append(f"background.start:{action.benchmark.lower()}")
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="started",
            started_at=_iso(time.time()),
            finished_at=_iso(time.time()),
            duration_ms=0,
        )

    def cleanup(self, action: BenchBaseAction) -> ActionResult:
        self.calls.append("cleanup")
        self.events.append("background.cleanup")
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="cleaned",
            started_at=_iso(time.time()),
            finished_at=_iso(time.time()),
            duration_ms=0,
        )


def make_runner(observed_value: float, action_result: ActionResult) -> tuple[DirectRunner, FakeDispatcher]:
    provider = PhaseAwareProvider(observed_value)
    sampler = MetricsSampler(PhaseAwareCollector(provider), interval_seconds=0.005)
    dispatcher = FakeDispatcher(action_result)
    registry = EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml")
    return DirectRunner(
        dispatcher=dispatcher,
        sampler=sampler,
        evaluator=IncidentEvaluator(registry),
    ), dispatcher


def action_result(success: bool = True) -> ActionResult:
    return ActionResult(
        action_id="sql-1",
        action_type="sql",
        success=success,
        status="succeeded" if success else "failed",
        started_at=_iso(time.time()),
        finished_at=_iso(time.time()),
        duration_ms=10,
        error_code=None if success else 1064,
        error_message=None if success else "syntax error",
    )


def request() -> DirectReproductionRequest:
    return DirectReproductionRequest(
        incident_id="direct-m7",
        action=SQLAction(
            action_id="sql-1",
            target_node="qps_drop",
            database="tpcc10_test",
            sql="SELECT 1",
            duration_sec=1,
        ),
        oracle_rule_id="qps_drop",
        baseline_seconds=0.03,
        observe_seconds=0.03,
        recovery_seconds=0.01,
    )


def background_request(benchmark: str) -> DirectReproductionRequest:
    return request().model_copy(
        update={
            "background_workload": BenchBaseAction(
                action_id=f"{benchmark}-background",
                target_node="tpcc10_test",
                benchmark=benchmark,
                database="tpcc10_test",
                terminals=4,
                rate=10,
                duration_sec=60,
                config_path="benchbase-config.xml",
                jar_path="benchbase.jar",
                results_dir="artifacts/benchbase",
            )
        }
    )


def test_direct_success_closed_loop_returns_experiment_success() -> None:
    runner, dispatcher = make_runner(69, action_result())

    result = runner.run(request())

    assert result.status == ResultStatus.EXPERIMENT_SUCCESS
    assert result.evaluation is not None and result.evaluation.hit is True
    assert result.state.phase.value == "success"
    assert dispatcher.calls == ["validate", "start", "execute", "cleanup"]
    assert any(sample.phase == ExperimentPhase.BASELINE for sample in result.timeline.samples)
    assert any(sample.phase == ExperimentPhase.OBSERVING for sample in result.timeline.samples)


def test_successful_action_but_oracle_miss_is_experiment_miss() -> None:
    runner, _ = make_runner(90, action_result())

    result = runner.run(request())

    assert result.status == ResultStatus.EXPERIMENT_MISS
    assert result.action_result is not None and result.action_result.success is True
    assert result.evaluation is not None and result.evaluation.hit is False
    assert result.state.phase.value == "cleaning_up"


def test_action_failure_is_system_error_not_experiment_miss() -> None:
    runner, dispatcher = make_runner(69, action_result(success=False))

    result = runner.run(request())

    assert result.status == ResultStatus.SYSTEM_ERROR
    assert result.action_result is not None and result.action_result.success is False
    assert result.evaluation is None
    assert result.state.phase.value == "cleaning_up"
    assert "cleanup" in dispatcher.calls


def test_three_repeated_manual_direct_runs_are_stable() -> None:
    statuses = []
    for index in range(3):
        runner, _ = make_runner(69, action_result())
        result = runner.run(request().model_copy(update={"incident_id": f"direct-m7-{index}"}))
        statuses.append(result.status)

    assert statuses == [ResultStatus.EXPERIMENT_SUCCESS] * 3


def test_tpcc_background_workload_starts_before_injection_and_is_cleaned_up() -> None:
    runner, dispatcher = make_runner(69, action_result())

    result = runner.run(background_request("tpcc"))

    assert result.status == ResultStatus.EXPERIMENT_SUCCESS
    assert result.background_workload_result is not None
    assert result.background_workload_result.success is True
    assert dispatcher.benchbase.calls == ["start", "cleanup"]
    assert dispatcher.events.index("background.start:tpcc") < dispatcher.events.index("action.start")
    assert dispatcher.events.index("background.cleanup") > dispatcher.events.index("action.cleanup")
    assert result.state.step_results["workload_starting"]["status"] == "background_started"


def test_tpch_background_workload_uses_the_same_m7_lifecycle() -> None:
    runner, dispatcher = make_runner(69, action_result())

    result = runner.run(background_request("tpch"))

    assert result.status == ResultStatus.EXPERIMENT_SUCCESS
    assert dispatcher.benchbase.calls == ["start", "cleanup"]
    assert "background.start:tpch" in dispatcher.events


def test_m7_rejects_non_tpcc_tpch_background_benchmark() -> None:
    runner, dispatcher = make_runner(69, action_result())
    invalid = background_request("ycsb")

    result = runner.run(invalid)

    assert result.status == ResultStatus.SYSTEM_ERROR
    assert result.state.phase.value == "cleaning_up"
    assert dispatcher.benchbase.calls == []


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _real_tpcc_config(tmp_path: Path, env: dict[str, str]) -> Path:
    source = Path(".tools/benchbase-main/target/benchbase-mysql/config/mysql/sample_tpcc_config.xml")
    text = source.read_text(encoding="utf-8")
    host = env["DBMAGS_MYSQL_HOST"]
    port = env["DBMAGS_MYSQL_PORT"]
    database = env["DBMAGS_MYSQL_DB"]
    user = html.escape(env["DBMAGS_MYSQL_USER"])
    password = html.escape(env["DBMAGS_MYSQL_PASSWORD"])
    url = html.escape(
        f"jdbc:mysql://{host}:{port}/{database}?"
        "rewriteBatchedStatements=true&allowPublicKeyRetrieval=true&sslMode=DISABLED"
    )
    text = re.sub(r"<url>.*?</url>", f"<url>{url}</url>", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<username>.*?</username>", f"<username>{user}</username>", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<password>.*?</password>", f"<password>{password}</password>", text, count=1, flags=re.DOTALL)
    destination = tmp_path / "tpcc10_test_config.xml"
    destination.write_text(text, encoding="utf-8")
    return destination


@pytest.mark.integration
def test_real_mysql_tpcc_background_and_m7_injection(tmp_path: Path) -> None:
    """Run the complete M7 path against the local MySQL and BenchBase TPCC.

    This is intentionally opt-in because TPCC writes to the configured database.
    The test uses the DBMAGS_* values from the repository .env file and creates a
    temporary BenchBase XML with those connection values.
    """
    if os.environ.get("DBMAGS_M7_RUN_REAL") != "1":
        pytest.skip("set DBMAGS_M7_RUN_REAL=1 to run real MySQL + BenchBase TPCC")

    dotenv = _read_dotenv(Path(".env"))
    required = {
        "DBMAGS_MYSQL_HOST",
        "DBMAGS_MYSQL_PORT",
        "DBMAGS_MYSQL_USER",
        "DBMAGS_MYSQL_PASSWORD",
        "DBMAGS_MYSQL_DB",
    }
    missing = required - dotenv.keys()
    if missing:
        pytest.fail(f".env is missing required database settings: {sorted(missing)}")

    config = MySQLConfig(
        host=dotenv["DBMAGS_MYSQL_HOST"],
        port=int(dotenv["DBMAGS_MYSQL_PORT"]),
        user=dotenv["DBMAGS_MYSQL_USER"],
        password=dotenv["DBMAGS_MYSQL_PASSWORD"],
        database=dotenv["DBMAGS_MYSQL_DB"],
        connect_timeout=5,
    )
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"local MySQL is not reachable: {exc}")

    jar = Path(".tools/benchbase-main/target/benchbase-mysql/benchbase.jar").resolve()
    if not jar.is_file():
        pytest.skip(f"BenchBase jar does not exist: {jar}")
    benchbase_config = _real_tpcc_config(tmp_path, dotenv)
    results_dir = tmp_path / "benchbase-results"

    adapter = MySQLAdapter(config)
    dispatcher = ActionDispatcher(adapter)
    collector = MetricsCollector({"mysql": MySQLMetricsProvider(adapter)})
    sampler = MetricsSampler(collector, interval_seconds=0.5)
    registry = EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml")
    runner = DirectRunner(
        dispatcher=dispatcher,
        sampler=sampler,
        evaluator=IncidentEvaluator(registry),
    )
    request = DirectReproductionRequest(
        incident_id="real-m7-tpcc",
        action=SQLAction(
            action_id="real-m7-sleep",
            target_node="connections_up",
            database=config.database,
            sql="SELECT SLEEP(3)",
            concurrency=3,
            execution_mode="concurrent",
            duration_sec=3,
            timeout_sec=10,
        ),
        background_workload=BenchBaseAction(
            action_id="real-tpcc-background",
            target_node="traffic_surge",
            benchmark="tpcc",
            database=config.database,
            terminals=1,
            rate=10,
            duration_sec=60,
            config_path=str(benchbase_config),
            jar_path=str(jar),
            results_dir=str(results_dir),
        ),
        oracle_rule_id="connections_up",
        baseline_seconds=3.0,
        observe_seconds=0.5,
        recovery_seconds=0.5,
    )

    result = runner.run(request)

    assert result.action_result is not None and result.action_result.success is True
    assert result.background_workload_result is not None
    assert result.background_workload_result.success is True
    assert result.state.phase.value == "success"
    assert result.evaluation is not None and result.evaluation.hit is True
    assert dispatcher.benchbase.process is None
