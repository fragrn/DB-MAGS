import time
import unittest
from unittest.mock import patch

from post_agent_support.config import RuntimeConfig
from post_agent_support.dag import build_task_dag
from post_agent_support.executor import Executor


class ExecutorDagDependencyTests(unittest.TestCase):
    def test_dict_edges_run_dependent_tasks_after_predecessors(self):
        dag = build_task_dag(
            [
                {"task_id": "first", "actions": [{"kind": "stub"}]},
                {"task_id": "independent", "actions": [{"kind": "stub"}]},
                {"task_id": "last", "actions": [{"kind": "stub"}]},
            ],
            dependencies=[["first", "last"]],
        )
        events = []
        lock = __import__("threading").Lock()

        def run_action(_kind, _action, task_id):
            with lock:
                events.append((task_id, "start", time.monotonic()))
            if task_id == "first":
                time.sleep(0.03)
            with lock:
                events.append((task_id, "end", time.monotonic()))
            return {"ok": True}

        executor = Executor(RuntimeConfig())
        with patch.object(executor, "_run_action", side_effect=run_action):
            trace = executor.execute(dag, max_duration_sec=2)

        self.assertEqual({task.status for task in trace.tasks.values()}, {"completed"})
        first_end = next(timestamp for task, event, timestamp in events if task == "first" and event == "end")
        last_start = next(timestamp for task, event, timestamp in events if task == "last" and event == "start")
        self.assertGreaterEqual(last_start, first_end)
        self.assertIn("independent", trace.tasks)


if __name__ == "__main__":
    unittest.main()
