import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from InputAnalysisAgent.codex_controller import ExperimentController


class ControllerTests(unittest.TestCase):
    def exercise(self, success, reflections):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mock()
            root = Path(tmp)
            runtime.create.return_value = {"run_dir":str(root)}
            runtime.status.side_effect = lambda _: {"attempt_dir":str(root),"config":{"target_database":"test"}}
            driver = Mock()
            def decision(prompt, directory, workdir):
                stage = directory.name
                if stage == "codex_calibration":
                    return {"decision":"accept","reasoning":"evidence"}
                if stage == "codex_evaluation":
                    return {"success":success}
                return {}
            driver.decide.side_effect = decision
            runtime.explain.return_value = {}
            runtime.execute.return_value = {}
            result = ExperimentController(driver, runtime).run("post",tmp,reflections)
            return result, runtime

    def test_partial_consumes_budget(self):
        result, runtime = self.exercise(False,3)
        self.assertEqual(len(result["attempts"]),4)
        self.assertEqual(runtime.revise.call_count,3)
        self.assertEqual(result["stop_reason"],"budget_exhausted")

    def test_success_stops(self):
        result, runtime = self.exercise(True,3)
        self.assertEqual(len(result["attempts"]),1)
        runtime.revise.assert_not_called()

    def test_zero_budget(self):
        result, runtime = self.exercise(False,0)
        self.assertEqual(len(result["attempts"]),1)
        runtime.revise.assert_not_called()
