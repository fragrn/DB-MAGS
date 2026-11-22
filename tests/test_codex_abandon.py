import tempfile
import unittest
from pathlib import Path

from InputAnalysisAgent.codex_runtime import CodexRuntime
from InputAnalysisAgent.hitl import write_json


class EarlyAbandonTests(unittest.TestCase):
    def test_can_abandon_with_evidence_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CodexRuntime()
            run = runtime.create("post", directory, max_reflections=1, allow_abandon=True)
            attempt = Path(run["attempt_dir"])
            write_json(attempt / "environment_blocker.json", {"reason": "required production-scale environment unavailable"})

            result = runtime.abandon(
                run["run_dir"],
                "The required environment is unavailable and this isolated reproduction cannot be representative.",
                ["environment_blocker.json"],
            )

            self.assertTrue(result["terminal"])
            self.assertEqual(result["outcome"], "abandoned")
            self.assertTrue(result["abandoned_before_execution"])
            self.assertEqual(result["phase_at_abandon"], "planning")

    def test_early_abandon_requires_opt_in_and_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CodexRuntime()
            run = runtime.create("post", directory, max_reflections=1)
            with self.assertRaisesRegex(ValueError, "abandon is disabled"):
                runtime.abandon(run["run_dir"], "blocked", ["missing.json"])

        with tempfile.TemporaryDirectory() as directory:
            runtime = CodexRuntime()
            run = runtime.create("post", directory, max_reflections=1, allow_abandon=True)
            with self.assertRaisesRegex(ValueError, "invalid abandonment evidence"):
                runtime.abandon(run["run_dir"], "blocked", ["missing.json"])


if __name__ == "__main__":
    unittest.main()
