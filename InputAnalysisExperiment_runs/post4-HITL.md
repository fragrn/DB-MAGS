# Post4 Human-in-the-loop Key Information

这次复现能到 15s，关键不是 agent 又发现了全新 SQL，而是 human-in-the-loop 补上了一个原帖没明说但很关键的数据分布假设：
token 在倒序主键扫描路径上极稀疏，并集中在旧数据低 id 区间。  这样改变了数据分布

## Purpose

This file records the extra information supplied by human-in-the-loop reasoning for the post4 reproduction. These assumptions do not modify the original forum post and should be treated as supplemental experimental assumptions used to make the synthetic reproduction closer to the reported 15-second latency.

## Original Post Evidence

- The post describes a slow MySQL query in a Redmine-like schema.
- The query filters issues with `LOWER(issues.subject) LIKE LOWER('%255392%')`.
- The query joins `issues` with `projects` and contains permission/module subqueries over `enabled_modules` and `members`.
- The query orders by `issues.id DESC` and applies `LIMIT 10`.
- The reported slow query latency is about 15 seconds.
- The post gives approximate table scale, including about 334,823 `issues`, 494 `projects`, and 1,350 users.

## Human-supplied Assumption

The key added assumption is:

> The token `255392` is extremely sparse along the `issues.id DESC` scan path and appears mainly in old, low-id rows. Therefore MySQL must scan a long range of high-id `issues` rows before finding 10 rows that match `LOWER(subject) LIKE '%255392%'`.

This assumption explains why the previous mechanism-level reproduction had the right SQL shape but only reached sub-second to low-second latency: its synthetic token distribution was still too friendly.

## Synthetic Data Design Added by HITL

- Use a separate database: `incident_repro_post4_10s`.
- Increase `issues` to 2,500,000 rows.
- Put token `255392` only in exactly ten low-id rows:
  - `50`
  - `100`
  - `150`
  - `200`
  - `250`
  - `300`
  - `350`
  - `400`
  - `450`
  - `500`
- Ensure high-id rows do not contain `255392`.
- Keep `LOWER(subject) LIKE '%255392%'` non-sargable.
- Preserve the Redmine-like permission predicates and `projects` join.
- Preserve `ORDER BY issues.id DESC LIMIT 10`.

## Expected Mechanism

The target mechanism is:

1. MySQL scans `issues` by reverse primary key order to satisfy `ORDER BY issues.id DESC LIMIT 10`.
2. The subject predicate cannot use an index because it is `LOWER(subject) LIKE '%255392%'`.
3. Matching rows are only in low-id/old rows.
4. MySQL must inspect a very large number of non-matching high-id rows.
5. Permission/module checks are evaluated along the scan path.
6. The resulting query latency approaches the original report's 15-second symptom.

## Resulting Blueprint Changes

- The final blueprint is stored in `blueprint.json`.
- The data generation logic is stored in `data_spec.json` and `preparation_result.json`.
- The calibration decision is stored in `calibration_result.json`.
- The execution and latency evidence is stored in `execution_bundle.json`.
- The final success judgment is stored in `evaluation_result.json`.

## Outcome

The reproduction succeeded with:

- Minimum latency: 12.47775 seconds
- p50 latency: 15.460504 seconds
- p95 latency: 20.83161 seconds
- Executions: 4
- Error count: 0

The evaluator judged:

- `symptom_hit = true`
- `mechanism_hit = true`
- `success = true`
- `plan_similarity = 0.97`

## Caveat

This reproduction is mechanism-level rather than an exact copy of the production incident. The original post does not explicitly state the exact placement or density of token `255392` in `issues.subject`; that distribution was inferred and supplied by HITL to explain and reproduce the reported 15-second runtime.
