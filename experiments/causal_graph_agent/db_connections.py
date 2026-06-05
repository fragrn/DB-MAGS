from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import MySQLConfig


@dataclass
class ConnectionCheck:
    ok: bool
    details: dict[str, Any]


def mysql_connect(config: MySQLConfig | None = None):
    cfg = config or MySQLConfig.from_env()
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("pymysql is required for MySQL connections.") from exc
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8",
        connect_timeout=10,
    )


def check_mysql_connection(config: MySQLConfig | None = None) -> ConnectionCheck:
    cfg = config or MySQLConfig.from_env()
    try:
        conn = mysql_connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE(), VERSION()")
                database, version = cur.fetchone()
        finally:
            conn.close()
        return ConnectionCheck(
            ok=True,
            details={
                "host": cfg.host,
                "port": cfg.port,
                "user": cfg.user,
                "database": database,
                "version": version,
            },
        )
    except Exception as exc:
        return ConnectionCheck(
            ok=False,
            details={
                "host": cfg.host,
                "port": cfg.port,
                "user": cfg.user,
                "database": cfg.database,
                "error": str(exc),
            },
        )
