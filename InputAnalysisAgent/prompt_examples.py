"""Local prompt examples used as lightweight format references."""

DATASPEC_FORMAT_REFERENCE = """Retrieved format reference for data_spec.
This example constrains JSON shape only. Do not copy its table names, SQL semantics,
data distribution, or evaluation intent; derive those from the DBA post.

{
  "data_spec": {
    "database": "synthetic_reproduction_db",
    "schema_sql": [
      "CREATE TABLE IF NOT EXISTS main_table (id INT PRIMARY KEY, category VARCHAR(64), ref_id INT, payload VARCHAR(255))",
      "CREATE TABLE IF NOT EXISTS ref_table (id INT PRIMARY KEY, status INT, name VARCHAR(64))"
    ],
    "generation_sql": [
      "INSERT INTO ref_table (id, status, name) SELECT n, MOD(n, 5), CONCAT('ref_', n) FROM generated_numbers WHERE n <= 100",
      "INSERT INTO main_table (id, category, ref_id, payload) SELECT n, CASE WHEN MOD(n, 1000)=0 THEN 'rare' ELSE 'common' END, 1 + MOD(n, 100), CONCAT('row_', n) FROM generated_numbers WHERE n <= {row_count}"
    ],
    "tables": [
      {
        "name": "main_table",
        "purpose": "primary synthetic table whose cardinality and value distribution drive the incident mechanism",
        "target_rows": 100000,
        "distribution_notes": "Describe skew, selectivity, ordering, clustering, or sparsity required by the reproduction."
      },
      {
        "name": "ref_table",
        "purpose": "reference table used by joins or filters in the reproduced query",
        "target_rows": 100,
        "distribution_notes": "Describe relationship cardinality or fanout required by the reproduction."
      }
    ],
    "constraints": {
      "cardinality": {"main_table": 100000, "ref_table": 100},
      "value_skew": {"main_table.category": "rare values are sparse and intentionally positioned by the agent"},
      "predicate_selectivity": {"main_table.category = 'rare'": 0.001},
      "join_selectivity": {"main_table.ref_id = ref_table.id": "many-to-one"}
    },
    "analyze_tables": ["main_table", "ref_table"],
    "calibration_queries": [
      {
        "sql": "SELECT m.* FROM main_table m JOIN ref_table r ON r.id = m.ref_id WHERE m.category = 'rare' ORDER BY m.id DESC LIMIT 10",
        "objective": "Verify that the prepared data and indexes produce the intended plan shape for the mechanism.",
        "expected_evidence": [
          "The plan examines the table or index range relevant to the suspected mechanism.",
          "The predicate or join behavior needed by the reproduction is visible in EXPLAIN.",
          "The plan evidence distinguishes this mechanism from a trivial point lookup."
        ]
      }
    ],
    "scale_strategy": {
      "initial_rows": 10000,
      "max_rows": 1000000,
      "growth_factor": 2.0,
      "max_rounds": 3
    }
  }
}

Hard format rules:
- data_spec.tables must be an array of objects, never an array of strings.
- Every table object must include at least name and purpose; include target_rows when scale matters.
- data_spec.calibration_queries must be an array of objects with sql, objective, and expected_evidence.
- calibration_queries.sql must be the original read-only SELECT/WITH query, not EXPLAIN SELECT.
"""
