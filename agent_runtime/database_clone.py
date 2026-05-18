from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from agent_runtime.env_loader import load_dotenv_files


def _env_default(name: str, fallback: str) -> str:
    value = os.getenv(name)
    return fallback if value is None else value


def connect(database: str | None = None):
    import pymysql

    load_dotenv_files()
    return pymysql.connect(
        host=_env_default("DBMAGS_MYSQL_HOST", "127.0.0.1"),
        port=int(_env_default("DBMAGS_MYSQL_PORT", "3306")),
        user=_env_default("DBMAGS_MYSQL_USER", "root"),
        passwd=_env_default("DBMAGS_MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        autocommit=True,
        local_infile=True,
    )


def ensure_database(cursor, name: str):
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


def drop_database(name: str):
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")


def clone_database(source: str, target: str):
    with connect() as conn:
        with conn.cursor() as cursor:
            ensure_database(cursor, target)
            cursor.execute(f"SHOW TABLES FROM `{target}`")
            for (table_name,) in cursor.fetchall():
                cursor.execute(f"DROP TABLE IF EXISTS `{target}`.`{table_name}`")
            cursor.execute(f"SHOW TABLES FROM `{source}`")
            for (table,) in cursor.fetchall():
                cursor.execute(f"CREATE TABLE `{target}`.`{table}` LIKE `{source}`.`{table}`")
                cursor.execute(f"INSERT INTO `{target}`.`{table}` SELECT * FROM `{source}`.`{table}`")


def unique_database_name(base_name: str, suffix: str) -> str:
    return f"{base_name}_run_{suffix}"
