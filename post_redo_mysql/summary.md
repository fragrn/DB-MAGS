# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_redo_mysql`
- Posts processed: 9
- success: 0
- partial: 0
- blocked: 0
- abandoned: 9
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| abandoned | long_transaction | 252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query | Stopped after 2 attempt(s): (1146, "Table 'post_retry_long_transaction_252749_lock_wa_r291386_a2.generated_numbers' doesn't exist") |
| abandoned | resource_limitation | 130884-mysql-database-uses-too-much-cpu | Stopped after 2 attempt(s): (1146, "Table 'post_retry_resource_limitation_130884_mysq_r328875_a2.generated_numbers' doesn't exist") |
| abandoned | resource_limitation | 17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk | Stopped after 2 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | slowsql | how-to-optimize-very-slow-select-with-left-joins-over-big-tables | Stopped after 2 attempt(s): final JSON parse failed: unterminated JSON object: line 1 column 1 (char 0) |
| abandoned | slowsql | improve-mysql-query-performance-from-slow-query-log | Stopped after 2 attempt(s): unsafe blueprint TaskSpecs: DAG has no tasks |
| abandoned | slowsql | mysql-query-performance-query-schema-indexes | Stopped after 2 attempt(s): (1146, "Table 'post_retry_slowsql_mysql_query_performance_r541792_a2.generated_numbers' doesn't exist") |
| abandoned | too_many_connection | 142243-mysql-database-has-way-too-many-connections | Stopped after 2 attempt(s): final JSON parse failed: unterminated JSON object: line 1 column 1 (char 0) |
| abandoned | too_many_connection | 20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps | Stopped after 2 attempt(s): (1040, 'Too many connections') |
| abandoned | too_many_connection | 4717-too-many-connections | Stopped after 2 attempt(s): background workload failed: ['(1064, "You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near \'CONNECT TO DATABASE; INSERT INTO test_ |
