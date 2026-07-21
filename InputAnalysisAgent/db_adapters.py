"""Small DBMS adapters used by InputAnalysisAgent reproductions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POSTGRES_ALIASES = {"postgres", "postgresql"}
MYSQL_ALIASES = {"mysql", "mariadb", "percona"}
SQLSERVER_ALIASES = {"sqlserver", "sql_server", "mssql", "sql server"}


def normalize_dbms(value: str) -> str:
    dbms = str(value or "").strip().lower().replace("-", "_")
    if dbms in POSTGRES_ALIASES:
        return "postgresql"
    if dbms in MYSQL_ALIASES:
        return "mysql"
    if dbms in SQLSERVER_ALIASES:
        return "sqlserver"
    return dbms or "unknown"


def supported_execution_dbms() -> set[str]:
    return {"mysql", "postgresql", "sqlserver"}


@dataclass
class PostgresConfig:
    host: str = ""
    port: int = 5432
    user: str = ""
    password: str = ""
    maintenance_db: str = "postgres"
    psql_bin: str = "psql"

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        return cls(
            host=os.environ.get("DBMAGS_PG_HOST") or os.environ.get("PGHOST", ""),
            port=int(os.environ.get("DBMAGS_PG_PORT") or os.environ.get("PGPORT") or 5432),
            user=os.environ.get("DBMAGS_PG_USER") or os.environ.get("PGUSER", ""),
            password=os.environ.get("DBMAGS_PG_PASSWORD") or os.environ.get("PGPASSWORD", ""),
            maintenance_db=os.environ.get("DBMAGS_PG_MAINTENANCE_DB") or "postgres",
            psql_bin=os.environ.get("DBMAGS_PSQL_BIN") or "psql",
        )

    def args(self, database: str) -> list[str]:
        args = [self.psql_bin, "-v", "ON_ERROR_STOP=1", "-X", "-q"]
        if self.host:
            args += ["-h", self.host]
        if self.port:
            args += ["-p", str(self.port)]
        if self.user:
            args += ["-U", self.user]
        args += ["-d", database]
        return args

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.password:
            env["PGPASSWORD"] = self.password
        return env


class PostgresAdapter:
    def __init__(self, config: PostgresConfig | None = None):
        self.config = config or PostgresConfig.from_env()

    def execute(self, database: str, sql: str, *, timeout: float = 120.0, tuples_only: bool = False) -> str:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("PostgreSQL SQL must be a non-empty string")
        args = self.config.args(database)
        if tuples_only:
            args += ["-t", "-A"]
        args += ["-c", sql]
        completed = subprocess.run(
            args,
            env=self.config.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout

    def create_database_if_not_exists(self, database: str) -> None:
        _safe_pg_identifier(database)
        exists_sql = f"SELECT 1 FROM pg_database WHERE datname = '{_sql_literal(database)}'"
        exists = self.execute(self.config.maintenance_db, exists_sql, tuples_only=True).strip()
        if exists != "1":
            self.execute(self.config.maintenance_db, f'CREATE DATABASE "{database}"')

    def prepare(self, database: str, schema_sql: list[str], generation_sql: list[str], analyze_tables: list[str], row_count: int) -> dict[str, Any]:
        self.create_database_if_not_exists(database)
        executed: list[str] = []
        for sql in schema_sql:
            self.execute(database, sql, timeout=240)
            executed.append(sql)
        for sql in generation_sql:
            statement = sql.format(row_count=row_count)
            self.execute(database, statement, timeout=300)
            executed.append(statement)
        for table in analyze_tables:
            _safe_pg_identifier(table)
            self.execute(database, f'ANALYZE "{table}"', timeout=120)
        return {"database": database, "dbms": "postgresql", "statement_count": len(executed), "statements": executed}

    def explain(self, database: str, sql: str) -> dict[str, Any]:
        plan_sql = "EXPLAIN (FORMAT JSON) " + _strip_explain(sql)
        raw = self.execute(database, plan_sql, timeout=120, tuples_only=True).strip()
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return {"dbms": "postgresql", "sql": sql, "plan": parsed, "raw": raw}

    def schema(self, database: str) -> dict[str, Any]:
        sql = """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
        rows = self.execute(database, sql, tuples_only=True).splitlines()
        result: dict[str, Any] = {}
        for row in rows:
            if not row.strip():
                continue
            table, column, data_type, nullable = (row.split("|") + ["", "", "", ""])[:4]
            result.setdefault(table, {"columns": [], "indexes": []})["columns"].append(
                {"name": column, "type": data_type, "nullable": nullable}
            )
        index_sql = """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY tablename, indexname
        """
        for row in self.execute(database, index_sql, tuples_only=True).splitlines():
            if not row.strip():
                continue
            table, index, definition = (row.split("|", 2) + ["", "", ""])[:3]
            result.setdefault(table, {"columns": [], "indexes": []})["indexes"].append(
                {"name": index, "definition": definition}
            )
        return result

    def table_stats(self, database: str) -> list[dict[str, Any]]:
        sql = """
        SELECT relname, COALESCE(n_live_tup, 0), pg_total_relation_size(relid)
        FROM pg_stat_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
        """
        items = []
        for row in self.execute(database, sql, tuples_only=True).splitlines():
            if not row.strip():
                continue
            table, rows, bytes_ = (row.split("|") + ["0", "0"])[:3]
            items.append({"table_name": table, "estimated_rows": int(float(rows or 0)), "total_bytes": int(float(bytes_ or 0))})
        return items

    def db_metrics(self, database: str) -> dict[str, Any]:
        return {
            "dbms": "postgresql",
            "version": self.execute(database, "SELECT version()", tuples_only=True).strip(),
            "activity": self.execute(
                database,
                "SELECT state, wait_event_type, count(*) FROM pg_stat_activity GROUP BY state, wait_event_type ORDER BY count(*) DESC",
                tuples_only=True,
            ).splitlines(),
        }

    def run_sql_workload(self, action: dict[str, Any], *, task_id: str, round_dir: Path) -> dict[str, Any]:
        sql = str(action.get("sql") or "")
        database = str(action.get("database") or "")
        concurrency = max(1, int(action.get("concurrency") or 1))
        duration_sec = float(action.get("duration_sec") or 1)
        stop = threading.Event()
        latencies: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            deadline = time.time() + duration_sec
            while not stop.is_set() and time.time() < deadline:
                started = time.perf_counter()
                try:
                    self.execute(database, sql, timeout=max(5.0, duration_sec + 5.0))
                    with lock:
                        latencies.append((time.perf_counter() - started) * 1000.0)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=duration_sec + 10.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=2.0)
        artifact = round_dir / f"{_safe_file(task_id)}_postgres_raw_sql_latencies.json"
        artifact.write_text(json.dumps({"latencies_ms": latencies, "errors": errors}, indent=2), encoding="utf-8")
        return {
            "kind": "raw_sql_workload",
            "dbms": "postgresql",
            "database": database,
            "concurrency": concurrency,
            "duration_sec": duration_sec,
            "executions": len(latencies),
            "latency_ms": _latency_summary(latencies),
            "error_count": len(errors),
            "errors": errors[:5],
            "latency_artifact": str(artifact),
        }

    def run_transaction_script(self, action: dict[str, Any]) -> dict[str, Any]:
        database = str(action.get("database") or "")
        scripts = action.get("scripts") or []
        errors: list[str] = []
        executed = 0
        lock = threading.Lock()

        def run_script(script: dict[str, Any], worker_index: int) -> None:
            nonlocal executed
            statements: list[str] = []
            for step in script.get("steps") or []:
                if isinstance(step, str):
                    statements.append(step)
                elif isinstance(step, dict):
                    if step.get("sleep_sec") is not None:
                        statements.append(f"SELECT pg_sleep({float(step.get('sleep_sec') or 0)})")
                    elif step.get("sql"):
                        statements.append(str(step["sql"]))
            try:
                self.execute(database, ";\n".join(statements), timeout=float(action.get("duration_sec") or 30) + 20)
                with lock:
                    executed += len(statements)
            except Exception as exc:
                with lock:
                    errors.append(f"{script.get('role', 'script')}[{worker_index}]: {exc}")

        threads: list[threading.Thread] = []
        for script in scripts:
            concurrency = max(1, int(script.get("concurrency") or action.get("concurrency") or 1))
            for index in range(concurrency):
                thread = threading.Thread(target=run_script, args=(script, index))
                thread.start()
                threads.append(thread)
        for thread in threads:
            thread.join(timeout=float(action.get("duration_sec") or 30) + 25)
        result = {
            "kind": "raw_transaction_script",
            "dbms": "postgresql",
            "thread_count": len(threads),
            "executed_steps": executed,
            "error_count": len(errors),
            "errors": errors[:10],
        }
        if errors:
            raise RuntimeError(f"postgres raw_transaction_script failed: {result}")
        return result


@dataclass
class SqlServerConfig:
    host: str = "127.0.0.1"
    port: int = 1433
    user: str = "sa"
    password: str = "YourStrong!Passw0rd"
    sqlcmd_bin: str = "sqlcmd"
    client_mode: str = "docker"
    client_image: str = "mcr.microsoft.com/mssql-tools"

    @classmethod
    def from_env(cls) -> "SqlServerConfig":
        return cls(
            host=os.environ.get("DBMAGS_SQLSERVER_HOST") or "127.0.0.1",
            port=int(os.environ.get("DBMAGS_SQLSERVER_PORT") or 1433),
            user=os.environ.get("DBMAGS_SQLSERVER_USER") or "sa",
            password=os.environ.get("DBMAGS_SQLSERVER_PASSWORD") or "YourStrong!Passw0rd",
            sqlcmd_bin=os.environ.get("DBMAGS_SQLCMD_BIN") or "sqlcmd",
            client_mode=os.environ.get("DBMAGS_SQLSERVER_CLIENT") or "docker",
            client_image=os.environ.get("DBMAGS_SQLSERVER_CLIENT_IMAGE") or "mcr.microsoft.com/mssql-tools",
        )


class SqlServerAdapter:
    """Minimal T-SQL adapter backed by sqlcmd or the mssql-tools Docker image."""

    def __init__(self, config: SqlServerConfig | None = None):
        self.config = config or SqlServerConfig.from_env()

    def _command(self, database: str, sql: str) -> list[str]:
        server = f"{self.config.host},{self.config.port}"
        if self.config.client_mode == "docker":
            server = f"host.docker.internal,{self.config.port}"
            return [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                self.config.client_image,
                "/opt/mssql-tools/bin/sqlcmd",
                "-S",
                server,
                "-U",
                self.config.user,
                "-P",
                self.config.password,
                "-d",
                database,
                "-b",
                "-Q",
                sql,
            ]
        return [
            self.config.sqlcmd_bin,
            "-S",
            server,
            "-U",
            self.config.user,
            "-P",
            self.config.password,
            "-d",
            database,
            "-b",
            "-N",
            "false",
            "-Q",
            sql,
        ]

    def _file_command(self, database: str, script_path: Path) -> list[str]:
        server = f"{self.config.host},{self.config.port}"
        if self.config.client_mode == "docker":
            server = f"host.docker.internal,{self.config.port}"
            return [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{script_path.parent}:/work:ro",
                self.config.client_image,
                "/opt/mssql-tools/bin/sqlcmd",
                "-S",
                server,
                "-U",
                self.config.user,
                "-P",
                self.config.password,
                "-d",
                database,
                "-b",
                "-i",
                f"/work/{script_path.name}",
            ]
        return [
            self.config.sqlcmd_bin,
            "-S",
            server,
            "-U",
            self.config.user,
            "-P",
            self.config.password,
            "-d",
            database,
            "-b",
            "-N",
            "false",
            "-i",
            str(script_path),
        ]

    def execute(self, database: str, sql: str, *, timeout: float = 120.0) -> str:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL Server SQL must be a non-empty string")
        completed = subprocess.run(
            self._command(database, sql),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout

    def execute_script(self, database: str, script: str, *, timeout: float = 120.0) -> str:
        with tempfile.TemporaryDirectory(prefix="dbmags_sqlcmd_") as tmp:
            path = Path(tmp) / "script.sql"
            path.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                self._file_command(database, path),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout

    def create_database_if_not_exists(self, database: str) -> None:
        _safe_sqlserver_identifier(database)
        self.execute("master", f"IF DB_ID(N'{_sql_literal(database)}') IS NULL CREATE DATABASE [{database}]", timeout=180)

    def prepare(self, database: str, schema_sql: list[str], generation_sql: list[str], analyze_tables: list[str], row_count: int) -> dict[str, Any]:
        self.create_database_if_not_exists(database)
        executed: list[str] = []
        for sql in schema_sql:
            statement = self._translate_setup_sql(sql)
            if not statement:
                continue
            self.execute(database, statement, timeout=240)
            executed.append(statement)
        for sql in generation_sql:
            statement = self._translate_setup_sql(sql.format(row_count=row_count))
            if not statement:
                continue
            self.execute(database, statement, timeout=300)
            executed.append(statement)
        for table in analyze_tables:
            _safe_sqlserver_identifier(table)
            self.execute(database, f"UPDATE STATISTICS [{table}]", timeout=180)
        return {"database": database, "dbms": "sqlserver", "statement_count": len(executed), "statements": executed}

    @staticmethod
    def _translate_setup_sql(sql: str) -> str:
        """Translate common MySQL/Postgres idempotent DDL shorthand into T-SQL."""
        statement = str(sql).strip().rstrip(";")
        statement = re.sub(r"^\s*CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+\[?[A-Za-z_][A-Za-z0-9_]*\]?\s*;?\s*", "", statement, flags=re.I)
        statement = re.sub(r"^\s*USE\s+\[?[A-Za-z_][A-Za-z0-9_]*\]?\s*;?\s*", "", statement, flags=re.I)
        statement = re.sub(r"^\s*CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+\[?[A-Za-z_][A-Za-z0-9_]*\]?\s*;?\s*", "", statement, flags=re.I)
        if not statement.strip():
            return ""
        table_match = re.match(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+((?:dbo\.)?\[?[A-Za-z_][A-Za-z0-9_]*\]?)(\s*\(.*\))$",
            statement,
            flags=re.I | re.S,
        )
        if table_match:
            table = table_match.group(1).strip("[]")
            body = table_match.group(2)
            if "." not in table:
                table = "dbo." + table
            statement = f"IF OBJECT_ID(N'{table}', N'U') IS NULL CREATE TABLE {table} {body}"
        index_match = re.match(
            r"CREATE\s+(UNIQUE\s+)?(?:(CLUSTERED|NONCLUSTERED)\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\[?[A-Za-z_][A-Za-z0-9_]*\]?)\s+ON\s+((?:(?:\[?dbo\]?)\.)?\[?[A-Za-z_][A-Za-z0-9_]*\]?)(\s*\(.*\))$",
            statement,
            flags=re.I | re.S,
        )
        if index_match:
            unique = index_match.group(1) or ""
            clustered = (index_match.group(2) or "").upper()
            index = index_match.group(3).strip("[]")
            table = index_match.group(4).replace("[", "").replace("]", "")
            cols = index_match.group(5)
            if "." not in table:
                table = "dbo." + table
            statement = (
                f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'{index}' "
                f"AND object_id = OBJECT_ID(N'{table}')) CREATE {unique}{clustered + ' ' if clustered else ''}INDEX {index} ON {table} {cols}"
            )
        else:
            plain_index_match = re.match(
                r"CREATE\s+(UNIQUE\s+)?(?:(CLUSTERED|NONCLUSTERED)\s+)?INDEX\s+(\[?[A-Za-z_][A-Za-z0-9_]*\]?)\s+ON\s+((?:(?:\[?dbo\]?)\.)?\[?[A-Za-z_][A-Za-z0-9_]*\]?)(\s*\(.*\))$",
                statement,
                flags=re.I | re.S,
            )
            if plain_index_match:
                unique = plain_index_match.group(1) or ""
                clustered = (plain_index_match.group(2) or "").upper()
                index = plain_index_match.group(3).strip("[]")
                table = plain_index_match.group(4).replace("[", "").replace("]", "")
                cols = plain_index_match.group(5)
                if "." not in table:
                    table = "dbo." + table
                statement = (
                    f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'{index}' "
                    f"AND object_id = OBJECT_ID(N'{table}')) CREATE {unique}{clustered + ' ' if clustered else ''}INDEX {index} ON {table} {cols}"
                )
        statement = re.sub(r"\bSERIAL\b", "INT IDENTITY(1,1)", statement, flags=re.I)
        statement = re.sub(r"\bBOOLEAN\b", "BIT", statement, flags=re.I)
        statement = re.sub(r"\bNVARCHAR\s*\(\s*MAX\s*\)", "NVARCHAR(450)", statement, flags=re.I)
        # Synthetic SQL Server reproductions often start from MySQL/PostgreSQL-ish
        # TEXT columns. NVARCHAR(MAX) cannot be used as an index key, so keep the
        # translated type bounded enough for synthetic indexed predicates.
        statement = re.sub(r"\bTEXT\b", "NVARCHAR(450)", statement, flags=re.I)
        return statement

    def explain(self, database: str, sql: str) -> dict[str, Any]:
        stripped = _strip_sqlserver_explain(sql)
        showplan = "SET SHOWPLAN_TEXT ON\nGO\n" + stripped + "\nGO\nSET SHOWPLAN_TEXT OFF\nGO\n"
        raw = self.execute_script(database, showplan, timeout=120)
        return {"dbms": "sqlserver", "sql": sql, "plan": raw.splitlines(), "raw": raw}

    def schema(self, database: str) -> dict[str, Any]:
        sql = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
        result: dict[str, Any] = {}
        for row in _parse_sqlcmd_rows(self.execute(database, sql)):
            if len(row) < 4:
                continue
            table, column, data_type, nullable = row[:4]
            result.setdefault(table, {"columns": [], "indexes": []})["columns"].append(
                {"name": column, "type": data_type, "nullable": nullable}
            )
        index_sql = """
        SELECT OBJECT_NAME(object_id), name, type_desc
        FROM sys.indexes
        WHERE object_id > 0 AND name IS NOT NULL
        ORDER BY OBJECT_NAME(object_id), name
        """
        for row in _parse_sqlcmd_rows(self.execute(database, index_sql)):
            if len(row) < 3:
                continue
            table, index, type_desc = row[:3]
            result.setdefault(table, {"columns": [], "indexes": []})["indexes"].append(
                {"name": index, "type": type_desc}
            )
        return result

    def table_stats(self, database: str) -> list[dict[str, Any]]:
        sql = """
        SELECT t.name, SUM(p.rows)
        FROM sys.tables t
        JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0,1)
        GROUP BY t.name
        ORDER BY SUM(p.rows) DESC
        """
        items = []
        for row in _parse_sqlcmd_rows(self.execute(database, sql)):
            if len(row) >= 2:
                items.append({"table_name": row[0], "estimated_rows": int(float(row[1] or 0))})
        return items

    def db_metrics(self, database: str) -> dict[str, Any]:
        return {
            "dbms": "sqlserver",
            "version": self.execute(database, "SELECT @@VERSION").strip(),
            "requests": self.execute(
                database,
                "SELECT status, wait_type, COUNT(*) FROM sys.dm_exec_requests GROUP BY status, wait_type",
            ).splitlines(),
        }

    def run_sql_workload(self, action: dict[str, Any], *, task_id: str, round_dir: Path) -> dict[str, Any]:
        sql = str(action.get("sql") or "")
        database = str(action.get("database") or "")
        concurrency = max(1, int(action.get("concurrency") or 1))
        duration_sec = float(action.get("duration_sec") or 1)
        stop = threading.Event()
        latencies: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            deadline = time.time() + duration_sec
            while not stop.is_set() and time.time() < deadline:
                started = time.perf_counter()
                try:
                    self.execute(database, sql, timeout=max(10.0, duration_sec + 10.0))
                    with lock:
                        latencies.append((time.perf_counter() - started) * 1000.0)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=duration_sec + 20.0)
        stop.set()
        artifact = round_dir / f"{_safe_file(task_id)}_sqlserver_raw_sql_latencies.json"
        artifact.write_text(json.dumps({"latencies_ms": latencies, "errors": errors}, indent=2), encoding="utf-8")
        return {
            "kind": "raw_sql_workload",
            "dbms": "sqlserver",
            "database": database,
            "concurrency": concurrency,
            "duration_sec": duration_sec,
            "executions": len(latencies),
            "latency_ms": _latency_summary(latencies),
            "error_count": len(errors),
            "errors": errors[:5],
            "latency_artifact": str(artifact),
        }

    def run_transaction_script(self, action: dict[str, Any]) -> dict[str, Any]:
        database = str(action.get("database") or "")
        scripts = action.get("scripts") or []
        errors: list[str] = []
        executed = 0
        lock = threading.Lock()

        def run_script(script: dict[str, Any], worker_index: int) -> None:
            nonlocal executed
            statements: list[str] = []
            for step in script.get("steps") or []:
                if isinstance(step, str):
                    statements.append(step)
                elif isinstance(step, dict):
                    if step.get("sleep_sec") is not None:
                        statements.append(f"WAITFOR DELAY '00:00:{int(float(step.get('sleep_sec') or 0)):02d}'")
                    elif step.get("sql"):
                        statements.append(str(step["sql"]))
            try:
                self.execute(database, ";\n".join(statements), timeout=float(action.get("duration_sec") or 30) + 30)
                with lock:
                    executed += len(statements)
            except Exception as exc:
                with lock:
                    errors.append(f"{script.get('role', 'script')}[{worker_index}]: {exc}")

        threads = []
        for script in scripts:
            concurrency = max(1, int(script.get("concurrency") or action.get("concurrency") or 1))
            for index in range(concurrency):
                thread = threading.Thread(target=run_script, args=(script, index))
                thread.start()
                threads.append(thread)
        for thread in threads:
            thread.join(timeout=float(action.get("duration_sec") or 30) + 35)
        result = {"kind": "raw_transaction_script", "dbms": "sqlserver", "thread_count": len(threads), "executed_steps": executed, "error_count": len(errors), "errors": errors[:10]}
        if errors:
            raise RuntimeError(f"sqlserver raw_transaction_script failed: {result}")
        return result

def _strip_explain(sql: str) -> str:
    return re.sub(r"^\s*EXPLAIN(?:\s*\([^)]*\))?\s+", "", str(sql), flags=re.I).strip().rstrip(";")


def _safe_pg_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(value or "")):
        raise ValueError(f"unsafe PostgreSQL identifier: {value}")
    return value


def _safe_sqlserver_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(value or "")):
        raise ValueError(f"unsafe SQL Server identifier: {value}")
    return value


def _sql_literal(value: str) -> str:
    return str(value).replace("'", "''")


def _safe_file(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))


def _strip_sqlserver_explain(sql: str) -> str:
    return re.sub(r"^\s*(?:SET\s+SHOWPLAN_(?:TEXT|XML|ALL)\s+ON;?)\s*", "", str(sql), flags=re.I).strip().rstrip(";")


def _parse_sqlcmd_rows(output: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-"} or "rows affected" in stripped:
            continue
        if re.search(r"\s{2,}", stripped):
            rows.append([part.strip() for part in re.split(r"\s{2,}", stripped) if part.strip()])
    return rows[1:] if len(rows) > 1 else rows


def _latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "min": round(ordered[0], 3),
        "p50": round(ordered[(len(ordered) - 1) // 2], 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(ordered[-1], 3),
    }
