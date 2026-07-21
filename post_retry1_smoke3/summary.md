# InputAnalysisAgent Batch Reproduction Summary

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry1_smoke3`
- Posts processed: 3
- success: 0
- partial: 0
- blocked: 0
- abandoned: 3
- failed: 0

| Status | Category | Slug | Reason |
|---|---|---|---|
| abandoned | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Stopped after 2 attempt(s): Msg 1913, Level 16, State 1, Server 883f252a88e4, Line 1 The operation failed because an index or statistics with name 'idx_comm_action' already exists on table 'Communication'. |
| abandoned | blocking | 132851-database-frozen-on-alter-table | Stopped after 2 attempt(s): ERROR:  column "stock" of relation "cliente" does not exist LINE 1: ..., direccion, comuna, ciudad, codigo_pais, activo, stock, vig...                                                              ^ |
| abandoned | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | Stopped after 2 attempt(s): query_assessments[0].observed_plan_summary is required |
