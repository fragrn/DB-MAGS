import tempfile
import unittest
from pathlib import Path

from InputAnalysisAgent.codex_runtime import CodexRuntime


class ReflectionBudgetTests(unittest.TestCase):
    def test_budget_transitions(self):
        for budget in (0, 2, 3):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                runtime = CodexRuntime()
                run = runtime.create('post', directory, max_reflections=budget)
                root = Path(run['run_dir'])
                for number in range(1, budget + 2):
                    runtime._finish_attempt(root, 'partial')
                    result = runtime.advance(root)
                    self.assertEqual(result['reflections_used'], number - 1)
                    self.assertEqual(result['next_action'], 'finished' if number == budget + 1 else 'submit_reflection')
                    if number <= budget:
                        runtime._new_attempt(root, number + 1)

    def test_success_stops_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CodexRuntime()
            run = runtime.create('post', directory, max_reflections=3)
            runtime._finish_attempt(run['run_dir'], 'success')
            self.assertEqual(runtime.advance(run['run_dir'])['next_action'], 'finished')
            with self.assertRaises(ValueError):
                runtime.submit_reflection(run['run_dir'], {})

    def test_conflicting_budgets_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                CodexRuntime().create('post', directory, max_reflections=2, max_attempts=3)


if __name__ == '__main__':
    unittest.main()
