# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1`
- Posts processed: 4
- success: 0
- partial: 0
- blocked: 2
- abandoned: 2
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| abandoned | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Stopped after 4 attempt(s): attempt timed out after 150s |
| blocked | blocking | 132851-database-frozen-on-alter-table | Missing capabilities: Database environment for post_retry_blocking_132851_database_frozen_r896472_a2 |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | Missing capabilities: Target database environment |
| abandoned | long_transaction | 222262-performance-of-large-transactions-and-concurrency | Stopped after 4 attempt(s): invalid reproduction blueprint: fact.source must be one of ['agent_inference', 'explicit_post', 'human_input', 'post_hypothesis'] |
