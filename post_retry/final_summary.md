# Final InputAnalysisAgent Reproduction Report

- Output root: `/Users/neo/.codex/worktrees/1299/DB-MAGS/post_retry`
- Posts processed: 23
- success: 1
- partial: 0
- blocked: 13
- abandoned: 9
- failed: 0

## Per-Post Status

| Status | Category | Post | Final Diagnosis |
|---|---|---|---|
| blocked | blocking | 129302-unpredicatable-single-insert-performance-on-sql-server-table | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | blocking | 132851-database-frozen-on-alter-table | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | blocking | 284397-concurrent-update-statements-of-single-row-in-small-table-takes-minutes | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | long_transaction | 222262-performance-of-large-transactions-and-concurrency | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| success | long_transaction | 252749-lock-wait-timeout-exceeded-try-restarting-transaction-for-my-delete-query | Reproduction executed and evaluator reported success. |
| blocked | long_transaction | 72191-why-is-my-select-statement-so-slow | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | resource_limitation | 108454-postgres-4x-slower-than-it-was | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| abandoned | resource_limitation | 130884-mysql-database-uses-too-much-cpu | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| abandoned | resource_limitation | 17677-why-is-mysql-is-creating-so-many-temporary-tables-on-disk | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| blocked | resource_limitation | 220486-slow-query-with-resource-semaphore-wait-info | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | resource_limitation | 291670-sql-server-query-performance-severely-regresses-due-to-high-memory-use | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | slowsql | 224651-stored-procedure-infinite-looping-after-index-updates | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | slowsql | 297892-query-slow-when-a-sub-select-is-used | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| abandoned | slowsql | how-to-optimize-very-slow-select-with-left-joins-over-big-tables | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| abandoned | slowsql | improve-mysql-query-performance-from-slow-query-log | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| blocked | slowsql | increasing-work-mem-and-shared-buffers-on-postgres-9-2-significantly-slows-down | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| abandoned | slowsql | mysql-query-performance-query-schema-indexes | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| abandoned | slowsql | optimize-a-query-thats-running-slow-with-nested-loops-inner-ioin | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| blocked | slowsql | why-does-adding-a-top-1-dramatically-worsen-performance | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| blocked | slowsql | why-is-my-query-suddenly-slower-than-it-was-yesterday | Blocked before planning because the post requires a non-MySQL execution environment not available in InputAnalysisAgent v1. |
| abandoned | too_many_connection | 142243-mysql-database-has-way-too-many-connections | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| abandoned | too_many_connection | 20479-how-to-resolve-too-many-connections-and-fatal-error-in-mysql-running-on-vps | LLM API rejected planning request with HTTP 403 even after compacting post input. |
| abandoned | too_many_connection | 4717-too-many-connections | LLM API rejected planning request with HTTP 403 even after compacting post input. |

## Agent Changes Made

- Added `InputAnalysisAgent.batch` batch orchestration with per-post databases, max-attempt handling, checkpoint automation, summaries, and unsupported-capability recording.
- Added `agent.cli batch-run` entry point.
- Added batch input compaction: original post is preserved as `source_post.txt`, planner input as `planner_post.txt`, with `input_compaction.json` describing what was removed.
- Added schema repair for common LLM output shorthand: `data_spec.tables`, `constraints`, `scale_strategy`, `analyze_tables`, `argv -> command`, action databases, and `workload_spec.queries`.
- Added prompt instruction that batch-provided `metadata.target_database` must be used consistently.
- Fixed executor DAG edge handling for dict/list edge shapes used by InputAnalysisAgent task DAGs.

## Remaining Missing Capabilities / Blockers

- Non-MySQL posts need SQL Server and PostgreSQL execution adapters/environments before mechanism-level reproduction can run.
- Several MySQL posts are blocked by the configured LLM API returning HTTP 403 at planning step 1; this is outside MySQL executor behavior and needs API/provider-side resolution or a different configured model endpoint.
- Some resource/connection experiments may require safer process/container isolation if we later allow high-concurrency or OS-level fault injection.
