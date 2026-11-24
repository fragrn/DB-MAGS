"""M2 unit tests and local-MySQL acceptance checks."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from db_repro_agent.tools.database.base import ToolResult
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig, validate_readonly_sql


def test_tool_result_success_and_error_contract() -> None:
    from db_repro_agent.tools.database.base import utc_now

    started = utc_now()
    finished = utc_now()
    success = ToolResult.ok({"value": 1}, started, finished)
    error = ToolResult.failure(
        started_at=started,
        finished_at=finished,
        error_code=1146,
        error_message="table does not exist",
        error_type="ProgrammingError",
    )

    assert success.success is True
    assert success.data == {"value": 1}
    assert success.error_code is None
    assert error.success is False
    assert error.error_code == 1146
    assert error.error_message == "table does not exist"
    assert error.finished_at >= error.started_at


def test_mysql_config_reads_dbmags_environment() -> None:
    config = MySQLConfig.from_env(
        {
            "DBMAGS_MYSQL_HOST": "db.example",
            "DBMAGS_MYSQL_PORT": "3307",
            "DBMAGS_MYSQL_USER": "tester",
            "DBMAGS_MYSQL_PASSWORD": "secret",
            "DBMAGS_MYSQL_DB": "dbmags_tool_test",
        }
    )

    assert config.host == "db.example"
    assert config.port == 3307
    assert config.user == "tester"
    assert config.password == "secret"
    assert config.database == "dbmags_tool_test"


@pytest.mark.parametrize("sql", ["UPDATE users SET x=1", "DROP TABLE users", "CALL p()", ""])
def test_readonly_sql_rejects_writes_and_empty_statements(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_readonly_sql(sql)


def test_readonly_sql_accepts_probe_statements() -> None:
    for sql in ["SELECT 1", "SHOW GLOBAL STATUS", "DESCRIBE warehouse", "EXPLAIN SELECT 1", "WITH x AS (SELECT 1) SELECT * FROM x"]:
        validate_readonly_sql(sql)


def test_connection_failure_is_structured() -> None:
    adapter = MySQLAdapter(
        MySQLConfig(host="127.0.0.1", port=1, user="root", password="", database="tpcc10_test", connect_timeout=0.2)
    )

    result = adapter.execute_readonly("SELECT 1")

    assert result.success is False
    assert result.error_code is not None
    assert result.error_message
    assert result.error_type
    assert result.data is None


def _live_adapter_or_skip() -> MySQLAdapter:
    config = MySQLConfig.from_env()
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"local MySQL is not reachable: {exc}")
    return MySQLAdapter(config)


@pytest.mark.integration
def test_local_mysql_tool_layer_acceptance() -> None:
    adapter = _live_adapter_or_skip()
    database = os.environ.get("DBMAGS_MYSQL_DB", "tpcc10_test")

    select_one = adapter.execute_readonly("SELECT 1 AS value")
    schema = adapter.probe_schema(database)
    indexes = adapter.probe_indexes(database, "warehouse")
    row_counts = adapter.probe_row_counts(database)
    runtime = adapter.probe_runtime()
    explain = adapter.explain_sql("SELECT * FROM warehouse WHERE w_id = %s", (1,))

    assert select_one.success is True
    assert select_one.data[0]["value"] == 1
    assert schema.success is True
    assert any(table["table_name"] == "warehouse" for table in schema.data["tables"])
    assert indexes.success is True
    assert any(index["name"] == "PRIMARY" and index["column"] == "w_id" for index in indexes.data["indexes"])
    assert row_counts.success is True
    assert "warehouse" in row_counts.data["row_counts"]
    assert runtime.success is True
    assert "Threads_connected" in runtime.data["status"]
    assert explain.success is True
    assert explain.data


@pytest.mark.integration
def test_local_mysql_tool_layer_preserves_database_errors() -> None:
    adapter = _live_adapter_or_skip()
    database = os.environ.get("DBMAGS_MYSQL_DB", "tpcc10_test")

    result = adapter.execute_readonly(
        "SELECT * FROM definitely_missing_table_for_m2", None
    )

    assert result.success is False
    assert result.error_code is not None
    assert result.error_message
    assert "definitely_missing_table_for_m2" in result.error_message
    assert result.error_details is not None
    assert result.error_details.get("mysql_args")
    assert database
