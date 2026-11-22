import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from InputAnalysisAgent.db_adapters import SqlServerAdapter


class SqlServerWorkloadTransportTests(unittest.TestCase):
    def test_raw_sql_workload_uses_file_transport_for_multiline_batch(self):
        adapter = SqlServerAdapter()
        adapter.execute_script = Mock(return_value="")
        adapter.execute = Mock(side_effect=AssertionError("raw batch must not be passed with -Q"))

        with tempfile.TemporaryDirectory() as temporary:
            result = adapter.run_sql_workload(
                {
                    "database": "repro_test",
                    "sql": "SET NOCOUNT ON;\nDECLARE @i int = 0;\nWHILE @i < 1000 BEGIN SET @i += 1; END;",
                    "concurrency": 1,
                    "duration_sec": 0.02,
                },
                task_id="long_batch",
                round_dir=Path(temporary),
            )
            artifact_exists = Path(result["latency_artifact"]).exists()

        self.assertGreater(result["executions"], 0)
        adapter.execute_script.assert_called()
        self.assertEqual(adapter.execute.call_count, 0)
        self.assertTrue(artifact_exists)


if __name__ == "__main__":
    unittest.main()
