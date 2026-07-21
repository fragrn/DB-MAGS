# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1`
- Posts processed: 10
- success: 0
- partial: 0
- blocked: 6
- abandoned: 4
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| abandoned | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Stopped after 4 attempt(s): sqlserver TaskSpec execution failed: [{"task_id": "setup_environment", "status": "failed", "actions": [], "errors": ["sqlserver raw_transaction_script failed: {'kind': 'raw_transaction_script', 'dbms': 'sqlserver |
| blocked | blocking | 132851-database-frozen-on-alter-table | Missing capabilities: Target database environment |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | The reproduction failed due to an inability to establish the required database environment. Connection errors indicate the database 'neo' does not exist, preventing setup and workload execution. |
| blocked | long_transaction | 222262-performance-of-large-transactions-and-concurrency | The experiment failed due to database connection errors, preventing the workload from executing as intended. The reproduction cannot proceed without resolving these errors. |
| abandoned | long_transaction | 252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query | Stopped after 4 attempt(s): (1146, "Table 'post_retry_long_transaction_252749_lock_wa_r904374_a4.generated_numbers' doesn't exist") |
| blocked | long_transaction | 72191-why-is-my-select-statement-so-slow | The target database 'post_retry_long_transaction_72191_why_is_m_r991625_a4' does not exist, preventing further inspection or reproduction. |
| blocked | resource_limitation | 108454-postgres-4x-slower-than-it-was | Missing capabilities: Database environment inspection |
| abandoned | resource_limitation | 130884-mysql-database-uses-too-much-cpu | Stopped after 4 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | resource_limitation | 17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk | Stopped after 4 attempt(s): (1146, "Table 'post_retry_resource_limitation_17677_why_i_r211127_a4.generated_numbers' doesn't exist") |
| blocked | resource_limitation | 220486-slow-query-with-resource-semaphore-wait-info | Missing capabilities: Existing target database environment |
