# InputAnalysisAgent post_retry1 最终汇总

- 输出目录：`/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1`
- 总帖子数：23
- 成功：2；部分成功：0；阻塞：9；失败/放弃：12

## 每帖状态

| 状态 | 分类 | 帖子 | 尝试 | 主要原因 |
|---|---|---|---:|---|
| 失败/放弃 | blocking | `129302-unpredicatable-single-insert-performance-on-sql-server-table` | 4 | Stopped after 4 attempt(s): sqlserver TaskSpec execution failed: [{"task_id": "setup_environment", "status": "failed", "actions": [], "errors": ["sqlserver raw_transaction_script failed: {'kind': 'raw_transaction_script', 'dbms': 'sqlserver', 'thread_count': 1, 'executed_steps': 0, 'error_count': 1, 'errors': [\"config |
| 无法复现/阻塞 | blocking | `132851-database-frozen-on-alter-table` | 1 | Missing capabilities: Target database environment |
| 无法复现/阻塞 | blocking | `284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes` | 3 | The target database 'post_retry_blocking_284397_concurrent_upda_r450040_a3' does not exist, preventing schema creation and reproduction. |
| 无法复现/阻塞 | long_transaction | `222262-performance-of-large-transactions-and-concurrency` | 1 | The target database does not exist, preventing schema setup and reproduction. |
| 失败/放弃 | long_transaction | `252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query` | 4 | Stopped after 4 attempt(s): (1146, "Table 'post_retry_long_transaction_252749_lock_wa_r580446_a4.generated_numbers' doesn't exist") |
| 无法复现/阻塞 | long_transaction | `72191-why-is-my-select-statement-so-slow` | 1 | The target database does not exist, preventing setup and reproduction. |
| 无法复现/阻塞 | resource_limitation | `108454-postgres-4x-slower-than-it-was` | 1 | Missing capabilities: Database environment for reproduction |
| 失败/放弃 | resource_limitation | `130884-mysql-database-uses-too-much-cpu` | 4 | Stopped after 4 attempt(s): (1146, "Table 'post_retry_resource_limitation_130884_mysq_r686919_a4.generated_numbers' doesn't exist") |
| 失败/放弃 | resource_limitation | `17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk` | 4 | Stopped after 4 attempt(s): invalid reproduction blueprint: TaskSpecs that use SET GLOBAL require cleanup_actions |
| 失败/放弃 | resource_limitation | `220486-slow-query-with-resource-semaphore-wait-info` | 4 | Stopped after 4 attempt(s): sqlserver TaskSpec execution failed: [{"task_id": "setup_environment", "status": "failed", "actions": [], "errors": ["sqlserver raw_transaction_script failed: {'kind': 'raw_transaction_script', 'dbms': 'sqlserver', 'thread_count': 1, 'executed_steps': 0, 'error_count': 1, 'errors': [\"config |
| 失败/放弃 | resource_limitation | `291670-sql-server-query-performance-severely-regresses-due-to-high-memory-use` | 4 | Stopped after 4 attempt(s): Msg 195, Level 15, State 10, Server 883f252a88e4, Line 1 'MOD' is not a recognized built-in function name. |
| 成功 | slowsql | `224651-stored-procedure-infinite-looping-after-index-updates` | 3 | The reproduction successfully demonstrated the reported symptoms, including infinite looping in the stored procedure and deadlocks during execution. The mechanism involving index locking and trigger interactions was also validated. However, no specific execution plan similarity was provided for evaluation. |
| 无法复现/阻塞 | slowsql | `297892-query-slow-when-a-sub-select-is-used` | 1 | The target database does not exist, preventing schema inspection and further reproduction steps. |
| 失败/放弃 | slowsql | `how-to-optimize-very-slow-select-with-left-joins-over-big-tables` | 4 | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| 失败/放弃 | slowsql | `improve-mysql-query-performance-from-slow-query-log` | 4 | Stopped after 4 attempt(s): (1146, "Table 'post_retry_slowsql_improve_mysql_query_per_r282748_a4.generated_numbers' doesn't exist") |
| 无法复现/阻塞 | slowsql | `increasing-work-mem-and-shared-buffers-on-postgres-9-2-significantly-slows-down` | 1 | Missing capabilities: Existing target database |
| 失败/放弃 | slowsql | `mysql-query-performance-query-schema-indexes` | 4 | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| 无法复现/阻塞 | slowsql | `optimize-a-query-thats-running-slow-with-nested-loops-inner-ioin` | 2 | Missing capabilities: Database creation or access to the specified database |
| 成功 | slowsql | `why-does-adding-a-top-1-dramatically-worsen-performance` | 4 | The reproduction successfully demonstrated the reported symptom and mechanism. The query with TOP 1 exhibited a Nested Loops join with a Table Spool operator, while the query without TOP 1 used a Hash Match join. This aligns with the expected behavior of row goal optimization altering the query plan. |
| 无法复现/阻塞 | slowsql | `why-is-my-query-suddenly-slower-than-it-was-yesterday` | 2 | Missing capabilities: Existing database environment |
| 失败/放弃 | too_many_connection | `142243-mysql-database-has-way-too-many-connections` | 4 | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| 失败/放弃 | too_many_connection | `20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps` | 4 | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| 失败/放弃 | too_many_connection | `4717-too-many-connections` | 4 | Stopped after 4 attempt(s): (1146, "Table 'post_retry_too_many_connection_4717_too_ma_r974031_a4.generated_numbers' doesn't exist") |

## 成功复现

- `slowsql/224651-stored-procedure-infinite-looping-after-index-updates`：The reproduction successfully demonstrated the reported symptoms, including infinite looping in the stored procedure and deadlocks during execution. The mechanism involving index locking and trigger interactions was also validated. However, no specific execution plan similarity was provided for evaluation.
  - run_dir: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1/slowsql/224651-stored-procedure-infinite-looping-after-index-updates/attempt_3/20260721-154753_51823cba`
- `slowsql/why-does-adding-a-top-1-dramatically-worsen-performance`：The reproduction successfully demonstrated the reported symptom and mechanism. The query with TOP 1 exhibited a Nested Loops join with a Table Spool operator, while the query without TOP 1 used a Hash Match join. This aligns with the expected behavior of row goal optimization altering the query plan.
  - run_dir: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1/slowsql/why-does-adding-a-top-1-dramatically-worsen-performance/attempt_4/20260721-155543_2ece7f73`

## 主要失败/阻塞类型

- 多数 MySQL 合成数据 SQL 仍有 `generated_numbers` 未创建、SET GLOBAL 缺 cleanup、DAG 无任务等蓝图质量问题。
- 多数 SQL Server 帖子虽已可连接 Azure SQL Edge，但和完整 SQL Server 版本/优化器行为/DMV/等待类型仍不完全等价。
- 部分 PostgreSQL/SQL Server 帖子被 LLM 判定缺少原目标数据库或特定生产环境，因此走 blocked。
- 资源类、连接数类帖子有些已真实触发系统或连接错误，但评价闭环还不稳定，最终按 abandoned 记录。

## 本轮 Agent 通用修改

- 新增/完善 PostgreSQL adapter：psql 连接、建库准备、schema/table stats/db metrics、EXPLAIN、raw_sql_workload、raw_transaction_script。
- 新增/完善 SQL Server-compatible adapter：通过 Docker mssql-tools/sqlcmd 连接 Azure SQL Edge，支持建库准备、schema/table stats/db metrics、SHOWPLAN_TEXT EXPLAIN、raw_sql_workload、raw_transaction_script。
- 将 .env 追加 PostgreSQL 与 SQL Server-compatible 连接配置；SQL Server 当前使用 Azure SQL Edge 容器而不是完整 SQL Server 2022。
- Planner/Calibration tool loop 支持按 DBMS 路由 inspect_db_environment 和 explain_sql。
- 增强 blueprint canonicalization：confidence=null 默认 0.5，非法 fact.source 降级为 agent_inference，DataSpec/Calibration 格式继续强校验。
- Batch repair 增强：每次尝试使用带 nonce 的独立 database；修复 data_spec.tables 字符串数组、workload query dict、未绑定 SQL 参数、action duration 超预算、setup-only TaskSpec、action database 不一致。
- SQL Server DDL 翻译增强：支持 CREATE TABLE/INDEX IF NOT EXISTS、NONCLUSTERED INDEX、SERIAL/BOOLEAN/TEXT/NVARCHAR(MAX) 到 T-SQL 可执行形式，并忽略 DataSpec 中额外 CREATE DATABASE/USE/CREATE SCHEMA IF NOT EXISTS 前缀。
- 外部 DBMS 执行路径增加受安全检查保护的 raw_command 支持，用于资源类复现实验。
- Calibration trace 修复：explain_sql 工具失败时仍保留解析后的 arguments，避免误判为未调用；reject 判定允许 observed_plan_summary 为空。

## 需要后续补齐

- 给 MySQL 数据生成提供通用 numbers/sequence 辅助能力，避免 LLM 反复引用不存在的 `generated_numbers`。
- 给 SET GLOBAL 类 TaskSpec 自动要求/校验 cleanup 的 prompt 示例或受控修复机制。
- 若要复现 SQL Server 特有等待、AlwaysOn、SQL Server 2008/2022 优化器差异，需要完整 SQL Server 环境；当前 Azure SQL Edge 只能做部分机制级模拟。
- 对 connection/resource 类实验补更客观 evaluator，否则容易出现“现象触发了但报告未判成功”。
