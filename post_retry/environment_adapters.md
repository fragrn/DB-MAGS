# InputAnalysisAgent DBMS Adapter Notes

## PostgreSQL

- Local PostgreSQL is reachable through Homebrew `postgresql@18`.
- Verified version: PostgreSQL 18.4 on Apple Silicon.
- Added InputAnalysisAgent PostgreSQL support for:
  - create database if missing
  - schema/generation SQL execution
  - `ANALYZE`
  - schema/table stats/probe metrics
  - `EXPLAIN (FORMAT JSON)`
  - `raw_sql_workload`
  - `raw_transaction_script`
  - SQL background workload

## SQL Server

- Official SQL Server 2022 container was attempted with `--platform linux/amd64`.
- It failed on Apple Silicon with:
  - `Invalid mapping of address ... in reserved address space`
- Azure SQL Edge ARM64 container was started successfully as a SQL Server-compatible local environment:
  - container: `dbmags-azure-sql-edge`
  - port: `127.0.0.1:1433`
  - user: `sa`
- Host `sqlcmd` failed against Azure SQL Edge due TLS certificate parsing.
- Dockerized `mcr.microsoft.com/mssql-tools` `sqlcmd` successfully connected and returned `@@VERSION`.
- Added InputAnalysisAgent SQL Server-compatible support for:
  - create database if missing
  - T-SQL schema/generation execution
  - `UPDATE STATISTICS`
  - schema/table stats/probe metrics
  - `SET SHOWPLAN_TEXT ON` calibration
  - `raw_sql_workload`
  - `raw_transaction_script`
  - SQL background workload

Note: Azure SQL Edge is not a full SQL Server 2022 instance. It is useful for local T-SQL-compatible smoke tests, but SQL Server-specific optimizer, memory grant, AlwaysOn, and wait behavior may differ from full SQL Server on x86_64.
