from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Tuple


@contextmanager
def db_cursor(connection_method: str = "connection2", database: str | None = None) -> Iterator[Tuple[object, object]]:
    from Connection.Connection import Database

    db = Database(mysql_db=database) if database else Database()
    conn_factory = getattr(db, connection_method)
    conn, cur = conn_factory()
    try:
        yield conn, cur
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
