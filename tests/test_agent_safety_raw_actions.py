import unittest

from agent.config import RuntimeConfig
from agent.safety import SafetyChecker


class RawActionSafetyTests(unittest.TestCase):
    def check(self, action):
        dag = {"tasks": {"task": {"task_id": "task", "task_type": "test", "actions": [action]}}, "edges": [], "schedule": {}}
        return SafetyChecker(RuntimeConfig()).check(dag, max_duration_sec=10)

    def test_raw_sql_workload_rejects_drop_table(self):
        result = self.check({"kind": "raw_sql_workload", "sql": "DROP TABLE scratch", "duration_sec": 1})
        self.assertFalse(result.approved)
        self.assertTrue(any("dangerous SQL" in reason for reason in result.reasons))

    def test_raw_transaction_script_rejects_drop_table(self):
        result = self.check({"kind": "raw_transaction_script", "duration_sec": 1, "scripts": [{"steps": ["DROP TABLE scratch"]}]})
        self.assertFalse(result.approved)
        self.assertTrue(any("dangerous SQL" in reason for reason in result.reasons))

    def test_raw_transaction_script_scans_object_sql_steps(self):
        result = self.check({"kind": "raw_transaction_script", "duration_sec": 1, "scripts": [{"steps": [{"sql": "TRUNCATE TABLE scratch"}]}]})
        self.assertFalse(result.approved)
        self.assertTrue(any("dangerous SQL" in reason for reason in result.reasons))


if __name__ == "__main__":
    unittest.main()
