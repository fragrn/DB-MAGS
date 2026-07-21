from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from InputAnalysisAgent.batch import repair_blueprint_payload, run_batch
from InputAnalysisAgent.cli import main as cli_main


class FakeRuntime:
    def run(self, post, *, metadata, output_root, interaction):
        run_dir = Path(output_root) / "fake_run"
        run_dir.mkdir(parents=True)
        evaluation = {
            "symptom_hit": True,
            "mechanism_hit": True,
            "plan_similarity": 0.9,
            "success": True,
            "reason": "ok",
            "unmatched_conditions": [],
            "evidence": {},
        }
        (run_dir / "evaluation_result.json").write_text(json.dumps(evaluation) + "\n")
        return {"run_id": "fake_run", "run_dir": str(run_dir), "status": "completed", "evaluation": evaluation}


class BatchRunTests(unittest.TestCase):
    def test_batch_run_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root = root / "post"
            (input_root / "slowsql").mkdir(parents=True)
            (input_root / "slowsql" / "one.txt").write_text("slow query")
            summary = run_batch(input_root=input_root, output_root=root / "post_retry", runtime=FakeRuntime())
            self.assertEqual(summary["counts"]["success"], 1)
            self.assertTrue((root / "post_retry" / "summary.json").exists())

    def test_repair_normalizes_common_shapes(self):
        payload = {
            "environment_spec": {"database": "old"},
            "data_spec": {
                "database": "old",
                "tables": ["items"],
                "constraints": ["bad"],
                "analyze_tables": ["db.items"],
                "scale_strategy": {"initial_rows": 0, "max_rows": 0, "growth_factor": 1, "max_rounds": 0},
            },
            "experiment_request": {"target_database": "old"},
            "task_specs": [{"actions": [{"kind": "raw_command", "argv": ["echo", "ok"], "database": "old", "duration_sec": 1}]}],
        }
        repaired, changes = repair_blueprint_payload(payload, "new_db")
        self.assertTrue(changes)
        self.assertEqual(repaired["data_spec"]["database"], "new_db")
        self.assertEqual(repaired["data_spec"]["tables"][0]["name"], "items")
        self.assertEqual(repaired["data_spec"]["analyze_tables"], ["items"])
        self.assertEqual(repaired["task_specs"][0]["actions"][0]["command"], ["echo", "ok"])

    def test_cli_batch_run_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root = root / "post"
            input_root.mkdir()
            with patch("InputAnalysisAgent.cli.run_batch", return_value={"counts": {}}) as batch:
                code = cli_main(["batch-run", "--input-root", str(input_root), "--output-root", str(root / "out")])
            self.assertEqual(code, 0)
            batch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
