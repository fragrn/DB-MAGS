from __future__ import annotations

import json
import sys
from pathlib import Path

from pymysql import OperationalError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.env_loader import load_dotenv_files
from Connection.Connection import Database


def classify_exception(exc: Exception) -> dict:
    if isinstance(exc, OperationalError):
        code = exc.args[0] if exc.args else None
        message = str(exc)
        mapping = {
            1045: "authentication_failed",
            1049: "database_not_found",
            2003: "host_or_port_unreachable",
            2005: "unknown_host",
        }
        return {"category": mapping.get(code, "operational_error"), "code": code, "message": message}
    return {"category": exc.__class__.__name__, "message": str(exc)}


def main() -> int:
    load_dotenv_files(ROOT)
    result = {
        "status": "failed",
        "connection_method": "connection2",
        "database": None,
        "select_1": None,
        "table_count": None,
        "error": None,
    }
    try:
        db = Database()
        conn, cur = db.connection2()
        try:
            cur.execute("SELECT 1")
            result["select_1"] = cur.fetchone()[0]
            cur.execute("SELECT DATABASE()")
            result["database"] = cur.fetchone()[0]
            cur.execute("SHOW TABLES")
            result["table_count"] = len(cur.fetchall())
            result["status"] = "ok"
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        result["error"] = classify_exception(exc)

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
