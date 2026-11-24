"""Bounded, manually specified SQL injections used by M9 and later stages."""

from __future__ import annotations

import re

from ..models.action import SQLAction


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("SQL identifier must contain only letters, digits and underscores")
    return value


def build_backup_action(
    action_id: str,
    database: str,
    *,
    source_table: str = "stock",
    backup_table: str = "dbmags_backup_shadow",
) -> SQLAction:
    """Create a one-shot table copy to generate bounded backup-like IO.

    The destination table must be isolated to the experiment database and
    should be removed by the experiment cleanup procedure afterwards.
    """
    source = _identifier(source_table)
    destination = _identifier(backup_table)
    return SQLAction(
        action_id=action_id,
        target_node="backup",
        database=database,
        sql=f"CREATE TABLE {destination} AS SELECT * FROM {source}",
        duration_sec=60,
        timeout_sec=90,
    )


def build_missing_index_action(
    action_id: str,
    database: str,
    *,
    table: str = "stock",
    filter_column: str = "s_data",
    sort_column: str = "s_data",
) -> SQLAction:
    """Query an unindexed filter/sort path without changing the schema."""
    table_name = _identifier(table)
    filter_name = _identifier(filter_column)
    sort_name = _identifier(sort_column)
    return SQLAction(
        action_id=action_id,
        target_node="missing_index",
        database=database,
        sql=(
            f"SELECT * FROM {table_name} "
            f"WHERE {filter_name} LIKE '%DBMAGS-MISSING-INDEX%' "
            f"ORDER BY {sort_name}"
        ),
        concurrency=2,
        execution_mode="concurrent",
        duration_sec=30,
        timeout_sec=60,
    )


def build_redo_log_pressure_action(
    action_id: str,
    database: str,
    *,
    table: str = "stock",
    id_column: str = "s_i_id",
    quantity_column: str = "s_quantity",
    concurrency: int = 4,
) -> SQLAction:
    """Generate bounded concurrent updates without changing redo configuration."""
    table_name = _identifier(table)
    id_name = _identifier(id_column)
    quantity_name = _identifier(quantity_column)
    return SQLAction(
        action_id=action_id,
        target_node="redo_log_pressure",
        database=database,
        sql=(
            f"UPDATE {table_name} SET {quantity_name} = MOD({quantity_name} + 1, 100) "
            f"WHERE {id_name} BETWEEN 1 AND 100000"
        ),
        concurrency=concurrency,
        execution_mode="concurrent",
        duration_sec=30,
        timeout_sec=90,
    )

