# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1`
- Posts processed: 23
- success: 2
- partial: 0
- blocked: 9
- abandoned: 12
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| abandoned | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Stopped after 4 attempt(s): sqlserver TaskSpec execution failed: [{"task_id": "setup_environment", "status": "failed", "actions": [], "errors": ["sqlserver raw_transaction_script failed: {'kind': 'raw_transaction_script', 'dbms': 'sqlserver |
| blocked | blocking | 132851-database-frozen-on-alter-table | Missing capabilities: Target database environment |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | The target database 'post_retry_blocking_284397_concurrent_upda_r450040_a3' does not exist, preventing schema creation and reproduction. |
| blocked | long_transaction | 222262-performance-of-large-transactions-and-concurrency | The target database does not exist, preventing schema setup and reproduction. |
| abandoned | long_transaction | 252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query | Stopped after 4 attempt(s): (1146, "Table 'post_retry_long_transaction_252749_lock_wa_r580446_a4.generated_numbers' doesn't exist") |
| blocked | long_transaction | 72191-why-is-my-select-statement-so-slow | The target database does not exist, preventing setup and reproduction. |
| blocked | resource_limitation | 108454-postgres-4x-slower-than-it-was | Missing capabilities: Database environment for reproduction |
| abandoned | resource_limitation | 130884-mysql-database-uses-too-much-cpu | Stopped after 4 attempt(s): (1146, "Table 'post_retry_resource_limitation_130884_mysq_r686919_a4.generated_numbers' doesn't exist") |
| abandoned | resource_limitation | 17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk | Stopped after 4 attempt(s): invalid reproduction blueprint: TaskSpecs that use SET GLOBAL require cleanup_actions |
| abandoned | resource_limitation | 220486-slow-query-with-resource-semaphore-wait-info | Stopped after 4 attempt(s): sqlserver TaskSpec execution failed: [{"task_id": "setup_environment", "status": "failed", "actions": [], "errors": ["sqlserver raw_transaction_script failed: {'kind': 'raw_transaction_script', 'dbms': 'sqlserver |
| abandoned | resource_limitation | 291670-sql-server-query-performance-severely-regresses-due-to-high-memory-use | Stopped after 4 attempt(s): Msg 195, Level 15, State 10, Server 883f252a88e4, Line 1 'MOD' is not a recognized built-in function name. |
| success | slowsql | 224651-stored-procedure-infinite-looping-after-index-updates | The reproduction successfully demonstrated the reported symptoms, including infinite looping in the stored procedure and deadlocks during execution. The mechanism involving index locking and trigger interactions was also validated. However, |
| blocked | slowsql | 297892-query-slow-when-a-sub-select-is-used | The target database does not exist, preventing schema inspection and further reproduction steps. |
| abandoned | slowsql | how-to-optimize-very-slow-select-with-left-joins-over-big-tables | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | slowsql | improve-mysql-query-performance-from-slow-query-log | Stopped after 4 attempt(s): (1146, "Table 'post_retry_slowsql_improve_mysql_query_per_r282748_a4.generated_numbers' doesn't exist") |
| blocked | slowsql | increasing-work-mem-and-shared-buffers-on-postgres-9-2-significantly-slows-down | Missing capabilities: Existing target database |
| abandoned | slowsql | mysql-query-performance-query-schema-indexes | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| blocked | slowsql | optimize-a-query-thats-running-slow-with-nested-loops-inner-ioin | Missing capabilities: Database creation or access to the specified database |
| success | slowsql | why-does-adding-a-top-1-dramatically-worsen-performance | The reproduction successfully demonstrated the reported symptom and mechanism. The query with TOP 1 exhibited a Nested Loops join with a Table Spool operator, while the query without TOP 1 used a Hash Match join. This aligns with the expect |
| blocked | slowsql | why-is-my-query-suddenly-slower-than-it-was-yesterday | Missing capabilities: Existing database environment |
| abandoned | too_many_connection | 142243-mysql-database-has-way-too-many-connections | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | too_many_connection | 20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | too_many_connection | 4717-too-many-connections | Stopped after 4 attempt(s): (1146, "Table 'post_retry_too_many_connection_4717_too_ma_r974031_a4.generated_numbers' doesn't exist") |
