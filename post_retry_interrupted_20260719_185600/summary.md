# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry`
- Posts processed: 4
- success: 0
- partial: 0
- blocked: 4
- abandoned: 0
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| blocked | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected sqlserver post before LLM planning. |
| blocked | blocking | 132851-database-frozen-on-alter-table | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
| blocked | long_transaction | 222262-performance-of-large-transactions-and-concurrency | Current InputAnalysisAgent v1 only executes MySQL-compatible reproductions; detected postgresql post before LLM planning. |
