"""M4 validation and safety tests."""

from __future__ import annotations

from pathlib import Path

from db_repro_agent.models.action import (
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
)
from db_repro_agent.runtime.action_validator import ActionValidator, ValidationDecision
from db_repro_agent.runtime.safety import SafetyChecker


class FakeSchemaProvider:
    def probe_schema(self, database: str):
        return {
            "database": database,
            "tables": [
                {"table_name": "warehouse", "columns": [{"name": "w_id"}, {"name": "w_ytd"}]},
                {"table_name": "items", "columns": [{"name": "id"}, {"name": "value"}]},
            ],
        }


def validator(tmp_path: Path) -> ActionValidator:
    return ActionValidator(
        target_database="dbmags_tool_test",
        schema_provider=FakeSchemaProvider(),
        allowed_target_nodes={"slow_query", "row_lock", "cpu_saturation"},
        max_duration_sec=300,
        allowed_path_roots=[tmp_path],
    )


def test_drop_database_is_rejected_by_safety() -> None:
    action = SQLAction(
        action_id="drop-db",
        target_node="slow_query",
        database="dbmags_tool_test",
        sql="DROP DATABASE mysql",
    )

    result = SafetyChecker(target_database="dbmags_tool_test").check(action)

    assert result.decision == ValidationDecision.REJECT
    assert any(issue.code == "DROP_DATABASE_FORBIDDEN" for issue in result.issues)


def test_information_schema_modification_is_rejected() -> None:
    action = SQLAction(
        action_id="system-db",
        target_node="slow_query",
        database="information_schema",
        sql="UPDATE tables SET table_name='x'",
    )

    result = SafetyChecker(target_database="dbmags_tool_test").check(action)

    assert result.decision == ValidationDecision.REJECT
    assert any(issue.code == "SYSTEM_DATABASE_FORBIDDEN" for issue in result.issues)


def test_unknown_column_requires_regeneration(tmp_path: Path) -> None:
    action = SQLAction(
        action_id="unknown-column",
        target_node="slow_query",
        database="dbmags_tool_test",
        sql="SELECT missing_column FROM warehouse WHERE w_id=1",
    )

    result = validator(tmp_path).validate(action)

    assert result.decision == ValidationDecision.REGENERATE
    assert any(issue.code == "UNKNOWN_COLUMN" for issue in result.issues)


def test_duration_over_budget_requires_regeneration(tmp_path: Path) -> None:
    action = SQLAction(
        action_id="too-long",
        target_node="slow_query",
        database="dbmags_tool_test",
        sql="SELECT w_id FROM warehouse",
        duration_sec=36000,
    )

    result = validator(tmp_path).validate(action)

    assert result.decision == ValidationDecision.REGENERATE
    assert any(issue.code == "DURATION_OVER_BUDGET" for issue in result.issues)


def test_raw_command_is_rejected_by_safety(tmp_path: Path) -> None:
    blade = tmp_path / "blade"
    blade.write_text("placeholder", encoding="utf-8")
    action = ChaosBladeAction(
        action_id="raw-command",
        target_node="cpu_saturation",
        resource="cpu",
        command="rm -rf /",
        duration_sec=3,
        blade_path=str(blade),
    )

    result = SafetyChecker(target_database="dbmags_tool_test", allowed_command_roots=[tmp_path]).check(action)

    assert result.decision == ValidationDecision.REJECT
    assert any(issue.code == "COMMAND_NOT_WHITELISTED" for issue in result.issues)


def test_normal_update_passes_validation_and_safety(tmp_path: Path) -> None:
    action = SQLAction(
        action_id="normal-update",
        target_node="slow_query",
        database="dbmags_tool_test",
        sql="UPDATE warehouse SET w_ytd=w_ytd+1 WHERE w_id=1",
    )

    validation = validator(tmp_path).validate(action)
    safety = SafetyChecker(target_database="dbmags_tool_test").check(action)

    assert validation.decision == ValidationDecision.PASS
    assert safety.decision == ValidationDecision.PASS


def test_benchbase_paths_are_checked_without_rewriting_action(tmp_path: Path) -> None:
    jar = tmp_path / "benchbase.jar"
    config = tmp_path / "config.xml"
    results = tmp_path / "results"
    jar.write_text("jar", encoding="utf-8")
    config.write_text("<parameters />", encoding="utf-8")
    action = BenchBaseAction(
        action_id="benchbase",
        target_node="slow_query",
        benchmark="tpcc",
        database="dbmags_tool_test",
        terminals=1,
        duration_sec=30,
        config_path=str(config),
        jar_path=str(jar),
        results_dir=str(results),
    )

    result = validator(tmp_path).validate(action)

    assert result.decision == ValidationDecision.PASS
    assert action.config_path == str(config)
    assert action.jar_path == str(jar)


def test_safety_does_not_rewrite_invalid_sql() -> None:
    sql = "DROP DATABASE mysql"
    action = SQLAction(
        action_id="immutable-sql",
        target_node="slow_query",
        database="dbmags_tool_test",
        sql=sql,
    )

    SafetyChecker(target_database="dbmags_tool_test").check(action)

    assert action.sql == sql
