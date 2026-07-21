# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry`
- Posts processed: 23
- success: 1
- partial: 0
- blocked: 13
- abandoned: 9
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| blocked | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | blocking | 132851-database-frozen-on-alter-table | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| blocked | long_transaction | 222262-performance-of-large-transactions-and-concurrency | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| success | long_transaction | 252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query | Yes: the conflicting `DELETE` returned `(1205, 'Lock wait timeout exceeded; try restarting transaction')` while a concurrent REPEATABLE READ transaction updated 2000 rows in the same `UPDATE_DATE < 20191015 AND COMPONENT_NAME = 'health'` ra |
| blocked | long_transaction | 72191-why-is-my-select-statement-so-slow | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | resource_limitation | 108454-postgres-4x-slower-than-it-was | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| abandoned | resource_limitation | 130884-mysql-database-uses-too-much-cpu | Stopped after 4 attempt(s): SQL background workload requires query strings |
| abandoned | resource_limitation | 17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| blocked | resource_limitation | 220486-slow-query-with-resource-semaphore-wait-info | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | resource_limitation | 291670-sql-server-query-performance-severely-regresses-due-to-high-memory-use | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | slowsql | 224651-stored-procedure-infinite-looping-after-index-updates | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | slowsql | 297892-query-slow-when-a-sub-select-is-used | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| abandoned | slowsql | how-to-optimize-very-slow-select-with-left-joins-over-big-tables | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| abandoned | slowsql | improve-mysql-query-performance-from-slow-query-log | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| blocked | slowsql | increasing-work-mem-and-shared-buffers-on-postgres-9-2-significantly-slows-down | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| abandoned | slowsql | mysql-query-performance-query-schema-indexes | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| abandoned | slowsql | optimize-a-query-thats-running-slow-with-nested-loops-inner-ioin | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| blocked | slowsql | why-does-adding-a-top-1-dramatically-worsen-performance | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | slowsql | why-is-my-query-suddenly-slower-than-it-was-yesterday | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| abandoned | too_many_connection | 142243-mysql-database-has-way-too-many-connections | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| abandoned | too_many_connection | 20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
| abandoned | too_many_connection | 4717-too-many-connections | Stopped after 4 attempt(s): tool-calling request failed at step 1, attempt 1: HTTP Error 403: Forbidden |
