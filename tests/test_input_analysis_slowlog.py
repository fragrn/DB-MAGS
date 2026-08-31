from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from agent.executor import Executor
from agent.types import ExecutionTrace
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint, ReproductionEvaluation
from InputAnalysisAgent.react import _tool_schemas
from InputAnalysisAgent.slowlog import SlowLogProbe, evaluate_slow_log_evidence, parse_slow_log
from tests.test_input_analysis_reproduction import blueprint_payload


SLOW_LOG_SAMPLE = """# Time: 2026-06-29T10:00:00.000000+08:00
# User@Host: root[root] @ localhost []  Id: 42
# Query_time: 0.000715  Lock_time: 0.000056 Rows_sent: 1  Rows_examined: 1456
SET timestamp=1782708000;
SELECT id FROM items WHERE tag = 'some_tag' AND status = 'active' LIMIT 1;
# Time: 2026-06-29T10:00:01.000000+08:00
# Query_time: 2.500000  Lock_time: 0.000010 Rows_sent: 1  Rows_examined: 2
SELECT id FROM items WHERE id = 1;
"""


def slowlog_blueprint() -> dict:
    payload = blueprint_payload()
    payload["incident_spec"]["summary"] = "Fast queries appear in the MySQL slow log."
    payload["incident_spec"]["mechanism"] = "log_queries_not_using_indexes logs fast scans"
    payload["data_spec"]["calibration_queries"][0]["sql"] = (
        "SELECT id FROM items WHERE tag = 'some_tag' AND status = 'active' LIMIT 1"
    )
    payload["task_specs"] = [
        {
            "task_id": "enable_and_generate_slow_log",
            "task_type": "observability",
            "actions": [
                {
                    "kind": "raw_transaction_script",
                    "database": "input_analysis_repro",
                    "duration_sec": 1,
                    "scripts": [
                        {
                            "role": "configure",
                            "autocommit": True,
                            "steps": [
                                {"sql": "SET GLOBAL long_query_time = 2"},
                                {"sql": "SET GLOBAL log_queries_not_using_indexes = 'ON'"},
                            ],
                        }
                    ],
                },
                {
                    "kind": "raw_sql_workload",
                    "database": "input_analysis_repro",
                    "sql": "SELECT id FROM items WHERE tag = 'some_tag' AND status = 'active' LIMIT 1",
                    "concurrency": 1,
                    "duration_sec": 1,
                },
            ],
            "cleanup_actions": [
                {
                    "kind": "raw_transaction_script",
                    "database": "input_analysis_repro",
                    "duration_sec": 1,
                    "scripts": [
                        {
                            "role": "restore",
                            "autocommit": True,
                            "steps": [
                                {"sql": "SET GLOBAL log_queries_not_using_indexes = 'OFF'"},
                                {"sql": "SET GLOBAL long_query_time = 10"},
                            ],
                        }
                    ],
                }
            ],
            "expected_metrics": {},
            "success_criteria": {},
            "risk_assessment": "high",
            "metadata": {"root_cause": "improper_sql"},
        }
    ]
    payload["risk_assessment"] = "high"
    payload["experiment_request"]["risk_level"] = "high"
    return payload


class SlowLogParserTests(unittest.TestCase):
    def test_react_exposes_global_variable_inspection(self):
        names = {item["function"]["name"] for item in _tool_schemas()}
        self.assertIn("inspect_mysql_global_variables", names)

    def test_parse_file_slow_log(self):
        entries = parse_slow_log(SLOW_LOG_SAMPLE)
        self.assertEqual(len(entries), 2)
        self.assertAlmostEqual(entries[0]["query_time_sec"], 0.000715)
        self.assertEqual(entries[0]["rows_examined"], 1456)
        self.assertIn("SELECT id FROM items", entries[0]["sql"])

    def test_parse_file_slow_log_tracks_database_and_thread_id(self):
        text = """# User@Host: root[root] @ localhost []  Id: 77
# Query_time: 11.500000  Lock_time: 0.100000 Rows_sent: 1 Rows_examined: 20
use tpcc_10W;
SET timestamp=1782708000;
SELECT * FROM order_line;
"""
        entry = parse_slow_log(text)[0]
        self.assertEqual(entry["database"], "tpcc_10W")
        self.assertEqual(entry["thread_id"], 77)

    def test_collect_filters_target_database_and_records_injection_variables(self):
        variables = {
            "slow_query_log": "ON",
            "log_output": "FILE",
            "slow_query_log_file": "/tmp/slow.log",
            "long_query_time": "10.000000",
            "log_queries_not_using_indexes": "ON",
            "min_examined_row_limit": "0",
        }
        probe = SlowLogProbe(RuntimeConfig())
        marker = {"variables_at_injection_start": variables, "file": {}, "table": {}}
        entries = [
            {"database": "tpcc", "query_time_sec": 12.0, "sql": "SELECT 1"},
            {"database": "other", "query_time_sec": 0.1, "sql": "SELECT 2"},
        ]
        with (
            patch.object(probe, "variables", return_value=(variables, "")),
            patch.object(probe, "_collect_file", return_value={"readable": True, "entries": entries}),
            patch.object(probe, "_collect_table", return_value=([], "not selected")),
        ):
            evidence = probe.collect(marker, target_database="tpcc")
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["entry_count"], 2)
        self.assertEqual(evidence["target_entry_count"], 1)
        self.assertEqual(evidence["target_entries"][0]["logging_reason"], "query_time_gte_long_query_time")
        self.assertEqual(evidence["entries"][1]["logging_reason"], "possible_log_queries_not_using_indexes")
        self.assertEqual(evidence["variables_at_injection_end"]["log_queries_not_using_indexes"], "ON")

    def test_start_capture_temporarily_enables_and_restore_disables_log(self):
        off = {"slow_query_log": "OFF", "slow_query_log_file": "", "log_output": "FILE"}
        on = {**off, "slow_query_log": "ON"}
        probe = SlowLogProbe(RuntimeConfig())
        with (
            patch.object(probe, "variables", side_effect=[(off, ""), (on, ""), (off, "")]),
            patch.object(probe, "_marker_from_variables", return_value={}),
            patch.object(probe, "_set_slow_query_log", return_value="") as setter,
        ):
            marker = probe.start_capture()
            restored = probe.restore(marker)
        self.assertTrue(marker["enabled_by_probe"])
        self.assertTrue(restored["restored"])
        self.assertEqual([call.args[0] for call in setter.call_args_list], [True, False])

    def test_file_probe_reads_only_bytes_after_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "slow.log"
            path.write_text("old content\n")
            marker = SlowLogProbe._file_marker(str(path))
            with path.open("a") as handle:
                handle.write(SLOW_LOG_SAMPLE)
            result = SlowLogProbe(RuntimeConfig())._collect_file(
                marker,
                {"slow_query_log_file": str(path)},
            )
            self.assertTrue(result["same_file"])
            self.assertEqual(len(result["entries"]), 2)

    def test_objective_evaluator_hits_fast_target_scan(self):
        evidence = {
            "available": True,
            "source": "FILE",
            "entries": parse_slow_log(SLOW_LOG_SAMPLE),
            "calibration": {"matched": True},
        }
        result = evaluate_slow_log_evidence(evidence, slowlog_blueprint())
        self.assertTrue(result["symptom_hit"])
        self.assertTrue(result["mechanism_hit"])
        self.assertEqual(result["matching_target_entry_count"], 1)
        self.assertEqual(result["fast_entry_count"], 1)

    def test_objective_evaluator_fails_without_matching_target(self):
        evidence = {
            "available": True,
            "entries": [{"query_time_sec": 0.1, "rows_examined": 100, "sql": "SELECT 1"}],
            "calibration": {"matched": True},
        }
        result = evaluate_slow_log_evidence(evidence, slowlog_blueprint())
        self.assertTrue(result["symptom_hit"])
        self.assertFalse(result["mechanism_hit"])


class SlowLogRuntimeTests(unittest.TestCase):
    def test_global_configuration_requires_cleanup(self):
        payload = slowlog_blueprint()
        payload["task_specs"][0]["cleanup_actions"] = []
        with self.assertRaisesRegex(ValueError, "cleanup_actions"):
            ReproductionBlueprint.from_dict(payload)

    def test_objective_evidence_overrides_optimistic_llm_evaluation(self):
        blueprint = ReproductionBlueprint.from_dict(slowlog_blueprint())
        execution = {
            "slow_log_evidence": {
                "available": True,
                "entries": [],
                "calibration": {"matched": True},
            }
        }
        optimistic = ReproductionEvaluation(True, True, 1.0, True, "LLM guessed success")
        runtime = ReproductionRuntime(RuntimeConfig(openai_api_key="test"))
        with patch("InputAnalysisAgent.runtime.evaluate_reproduction", return_value=optimistic):
            result = runtime._evaluate(blueprint, execution)
        self.assertFalse(result.success)
        self.assertFalse(result.mechanism_hit)
        self.assertIn("slow_log", result.evidence)

    def test_executor_runs_supported_cleanup_actions(self):
        executor = Executor(RuntimeConfig())
        dag = {
            "tasks": {
                "configure": {
                    "cleanup_actions": [
                        {
                            "kind": "raw_transaction_script",
                            "database": "testdb",
                            "duration_sec": 1,
                            "scripts": [],
                        }
                    ]
                }
            }
        }
        trace = ExecutionTrace()
        with patch.object(executor, "_run_action", return_value={"ok": True}) as action:
            executor._run_cleanup(dag, trace)
        action.assert_called_once()
        self.assertEqual(trace.cleanup_status, "completed")


if __name__ == "__main__":
    unittest.main()
