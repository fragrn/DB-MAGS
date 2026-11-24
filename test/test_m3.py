"""M3 tests and acceptance checks for ExecutableAction and Executor."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from db_repro_agent.execution.benchbase_executor import BenchBaseExecutor
from db_repro_agent.execution.chaos_executor import ChaosExecutor
from db_repro_agent.execution.command_executor import CommandExecutor
from db_repro_agent.execution.dispatcher import ActionDispatcher
from db_repro_agent.execution.sql_executor import SQLExecutor
from db_repro_agent.execution.transaction_executor import TransactionExecutor
from db_repro_agent.models.action import (
    ActionResultStatus,
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
    TransactionActor,
    TransactionStep,
)
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig


def test_action_models_cover_all_executable_types() -> None:
    sql = SQLAction(
        action_id="sql-1",
        target_node="slow_query",
        database="tpcc10_test",
        sql="SELECT SLEEP(2)",
        duration_sec=2,
        timeout_sec=5,
    )
    transaction = TransactionAction(
        action_id="lock-1",
        target_node="row_lock",
        database="dbmags_tool_test",
        actors=[
            TransactionActor(
                actor_id="holder",
                role="holder",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(sql="SELECT id FROM items WHERE id=1 FOR UPDATE", delay_after_sec=3),
                    TransactionStep(sql="COMMIT"),
                ],
            ),
            TransactionActor(
                actor_id="waiter",
                role="waiter",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(sql="UPDATE items SET value=value+1 WHERE id=1"),
                    TransactionStep(sql="COMMIT"),
                ],
            ),
        ],
        duration_sec=10,
    )
    benchbase = BenchBaseAction(
        action_id="bb-1",
        target_node="traffic_surge",
        benchmark="tpcc",
        database="tpcc10_test",
        terminals=4,
        rate=100,
        duration_sec=30,
        transaction_weights={"NewOrder": 45},
        config_path="config.xml",
        jar_path="benchbase.jar",
        results_dir="results",
    )
    chaos = ChaosBladeAction(
        action_id="cpu-1",
        target_node="cpu_saturation",
        resource="cpu",
        command="cpu load --cpu-percent 20 --timeout 3",
        duration_sec=3,
        blade_path="blade",
    )

    assert sql.type == "sql"
    assert transaction.actors[0].role == "holder"
    assert transaction.actors[1].role == "waiter"
    assert benchbase.terminals == 4
    assert chaos.resource == "cpu"


def test_benchbase_normalizes_jar_path_to_absolute_path(tmp_path: Path) -> None:
    jar = tmp_path / "benchbase.jar"
    config = tmp_path / "config.xml"
    jar.write_text("fixture", encoding="utf-8")
    config.write_text("<config />", encoding="utf-8")
    action = BenchBaseAction(
        action_id="bb-path",
        target_node="traffic_surge",
        benchmark="tpcc",
        database="tpcc10_test",
        terminals=1,
        duration_sec=1,
        config_path=str(config),
        jar_path=str(Path(".") / jar.name),
        results_dir=str(tmp_path / "results"),
    )
    executor = BenchBaseExecutor()
    normalized = executor._normalize_action(action.model_copy(update={"jar_path": str(jar)}))

    assert Path(normalized.jar_path).is_absolute()
    assert Path(normalized.jar_path) == jar.resolve()


def test_dispatcher_routes_four_action_types() -> None:
    dispatcher = ActionDispatcher()
    actions = [
        SQLAction(action_id="sql", target_node="n", database="db", sql="SELECT 1"),
        TransactionAction(
            action_id="tx",
            target_node="n",
            database="db",
            actors=[TransactionActor(actor_id="a", steps=[TransactionStep(sql="BEGIN")])],
        ),
        BenchBaseAction(
            action_id="bb",
            target_node="n",
            benchmark="tpcc",
            database="db",
            terminals=1,
            duration_sec=1,
            config_path="config.xml",
            jar_path="benchbase.jar",
            results_dir="results",
        ),
        ChaosBladeAction(
            action_id="chaos",
            target_node="n",
            resource="cpu",
            command="cpu load",
            duration_sec=1,
            blade_path="blade",
        ),
    ]

    assert type(dispatcher.executor_for(actions[0])) is SQLExecutor
    assert type(dispatcher.executor_for(actions[1])) is TransactionExecutor
    assert type(dispatcher.executor_for(actions[2])) is BenchBaseExecutor
    assert type(dispatcher.executor_for(actions[3])) is ChaosExecutor


def test_sql_executor_rejects_multiple_statements_before_database_call() -> None:
    class ExplodingAdapter:
        def connect(self, **kwargs):
            raise AssertionError("database must not be contacted")

    action = SQLAction(
        action_id="invalid-sql",
        target_node="n",
        database="db",
        sql="SELECT 1; DROP TABLE users",
    )

    with pytest.raises(ValueError):
        SQLExecutor(ExplodingAdapter()).validate(action)


def test_transaction_executor_rejects_duplicate_actor_ids() -> None:
    action = TransactionAction(
        action_id="invalid-tx",
        target_node="row_lock",
        database="db",
        actors=[
            TransactionActor(actor_id="same", steps=[TransactionStep(sql="BEGIN")]),
            TransactionActor(actor_id="same", steps=[TransactionStep(sql="COMMIT")]),
        ],
    )

    with pytest.raises(ValueError):
        TransactionExecutor().validate(action)


def test_command_executor_timeout_is_structured() -> None:
    result = CommandExecutor().run(
        action_id="timeout",
        action_type="command",
        command=["python", "-c", "import time; time.sleep(2)"],
        timeout_sec=0.05,
    )

    assert result.success is False
    assert result.status == ActionResultStatus.TIMED_OUT
    assert result.error_code == "TIMEOUT"


def test_chaos_executor_rejects_shell_control_syntax() -> None:
    action = ChaosBladeAction(
        action_id="unsafe",
        target_node="cpu",
        resource="cpu",
        command="cpu load; rm -rf /",
        duration_sec=1,
        blade_path="blade",
    )

    with pytest.raises(ValueError):
        ChaosExecutor().validate(action)


def _live_adapter_or_skip() -> MySQLAdapter:
    config = MySQLConfig.from_env()
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"local MySQL is not reachable: {exc}")
    return MySQLAdapter(config)


@pytest.mark.integration
def test_sql_action_select_sleep_duration() -> None:
    adapter = _live_adapter_or_skip()
    action = SQLAction(
        action_id="sleep-2",
        target_node="slow_query",
        database=os.environ.get("DBMAGS_MYSQL_DB", "tpcc10_test"),
        sql="SELECT SLEEP(2) AS slept",
        duration_sec=2,
        timeout_sec=10,
    )

    result = ActionDispatcher(adapter).run(action)

    assert result.success is True
    assert result.status == ActionResultStatus.SUCCEEDED
    assert result.duration_ms >= 1900


@pytest.mark.integration
def test_transaction_holder_waiter_row_lock() -> None:
    if os.environ.get("DBMAGS_M3_ALLOW_WRITE_TEST") != "1":
        pytest.skip("set DBMAGS_M3_ALLOW_WRITE_TEST=1 with an isolated test database")
    adapter = _live_adapter_or_skip()
    database = os.environ.get("DBMAGS_MYSQL_DB", "dbmags_tool_test")
    action = TransactionAction(
        action_id="holder-waiter",
        target_node="row_lock",
        database=database,
        actors=[
            TransactionActor(
                actor_id="holder",
                role="holder",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(sql="SELECT id FROM items WHERE id=1 FOR UPDATE", delay_after_sec=3),
                    TransactionStep(sql="COMMIT"),
                ],
            ),
            TransactionActor(
                actor_id="waiter",
                role="waiter",
                steps=[
                    TransactionStep(sql="BEGIN"),
                    TransactionStep(sql="UPDATE items SET value=value+1 WHERE id=1"),
                    TransactionStep(sql="COMMIT"),
                ],
            ),
        ],
        duration_sec=10,
    )

    result = ActionDispatcher(adapter).run(action)

    assert result.success is True
    waiter = next(item for item in result.data if item["actor_id"] == "waiter")
    assert waiter["success"] is True


@pytest.mark.integration
def test_benchbase_starts_has_pid_and_cleans_up() -> None:
    if os.environ.get("DBMAGS_M3_RUN_EXPERIMENTS") != "1":
        pytest.skip("set DBMAGS_M3_RUN_EXPERIMENTS=1 to run BenchBase")
    jar = Path(".tools/benchbase-main/target/benchbase-mysql/benchbase.jar").resolve()
    config = Path(".tools/benchbase-main/target/benchbase-mysql/config/mysql/sample_tpcc_config.xml").resolve()
    results = Path("experiment_runs/m3-benchbase").resolve()
    results.mkdir(parents=True, exist_ok=True)
    action = BenchBaseAction(
        action_id="tpcc-30s",
        target_node="traffic_surge",
        benchmark="tpcc",
        database=os.environ.get("DBMAGS_MYSQL_DB", "tpcc10_test"),
        terminals=1,
        duration_sec=30,
        transaction_weights={},
        config_path=str(config),
        jar_path=str(jar),
        results_dir=str(results),
    )

    executor = BenchBaseExecutor()
    started = executor.start(action)
    try:
        assert started.success is True
        assert started.metadata["pid"] > 0
        assert executor.process is not None
        assert executor.process.poll() is None
    finally:
        cleaned = executor.cleanup(action)
    assert cleaned.success is True
    assert executor.process is None


@pytest.mark.integration
def test_chaosblade_cpu_pressure_cleanup() -> None:
    if os.environ.get("DBMAGS_M3_RUN_EXPERIMENTS") != "1":
        pytest.skip("set DBMAGS_M3_RUN_EXPERIMENTS=1 to run ChaosBlade")
    blade = Path(".tools/chaosblade-1.8.0-darwin_arm64/blade").resolve()
    action = ChaosBladeAction(
        action_id="cpu-pressure",
        target_node="cpu_saturation",
        resource="cpu",
        command="cpu load --cpu-percent 20 --timeout 3",
        duration_sec=3,
        blade_path=str(blade),
    )

    executor = ChaosExecutor()
    started = executor.start(action)
    try:
        assert started.success is True
    finally:
        cleaned = executor.cleanup(action)
    assert cleaned.success is True
    assert executor.active_uid is None
