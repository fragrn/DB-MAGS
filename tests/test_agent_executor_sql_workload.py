import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from agent.config import RuntimeConfig
from agent.executor import Executor


class SqlWorkloadAutocommitTests(unittest.TestCase):
    def _run(self, action):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor
        pymysql = types.SimpleNamespace(connect=MagicMock(return_value=connection))
        executor = Executor(RuntimeConfig())
        with patch.dict(sys.modules, {"pymysql": pymysql}):
            result = executor._run_sql_workload({"sql": "UPDATE probe SET value=1 WHERE id=1", "duration_sec": 0.01, **action})
        return pymysql.connect, connection, result

    def test_raw_sql_workload_commits_dml_by_default(self):
        connect, connection, result = self._run({"database": "isolated_test"})

        self.assertTrue(connect.call_args.kwargs["autocommit"])
        self.assertGreaterEqual(result["executions"], 1)
        connection.close.assert_called()

    def test_raw_sql_workload_can_explicitly_disable_autocommit(self):
        connect, _connection, result = self._run({"database": "isolated_test", "autocommit": False})

        self.assertFalse(connect.call_args.kwargs["autocommit"])
        self.assertGreaterEqual(result["executions"], 1)


if __name__ == "__main__":
    unittest.main()
