from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from InputAnalysisAgent.hitl import (
    HumanDecision,
    HumanGateRequired,
    RunState,
    apply_controlled_patch,
    gate_reasons,
    load_state,
    save_state,
    write_json,
)
from InputAnalysisAgent.react import ReproductionPlanningError, canonicalize_blueprint_payload, plan_reproduction
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint
from InputAnalysisAgent.schemas import ReproductionEvaluation
from InputAnalysisAgent.cli import main as cli_main


def blueprint_payload(*, confidence: float = 0.9, risk: str = "low") -> dict:
    return {
        "incident_spec": {
            "dbms": "mysql",
            "dbms_version": "8.0",
            "summary": "An unindexed predicate produces excessive scanning.",
            "symptoms": ["slow query"],
            "mechanism": "full scan on an unindexed predicate",
            "facts": [
                {
                    "key": "query_shape",
                    "value": "LIKE with leading wildcard",
                    "source": "explicit_post",
                    "evidence": "The post includes LIKE '%token%'.",
                    "confidence": 1.0,
                }
            ],
            "assumptions": ["Synthetic data is acceptable"],
            "unknowns": [],
            "confidence": confidence,
        },
        "feasibility": {
            "level": "mechanism",
            "rationale": "MySQL scan behavior can be reproduced with synthetic data.",
            "missing_capabilities": [],
            "unmatched_conditions": ["Original production data is unavailable"],
            "confidence": confidence,
        },
        "environment_spec": {
            "dbms": "mysql",
            "database": "input_analysis_repro",
            "requirements": [],
            "isolation": "dedicated_test_database",
        },
        "data_spec": {
            "database": "input_analysis_repro",
            "schema_sql": [
                "CREATE TABLE IF NOT EXISTS items (id INT PRIMARY KEY, description VARCHAR(255))"
            ],
            "generation_sql": [],
            "tables": [{"name": "items", "row_count": 1000}],
            "constraints": {
                "cardinality": {"items": 1000},
                "predicate_selectivity": {"description": 0.001},
            },
            "analyze_tables": ["items"],
            "calibration_queries": [
                {
                    "sql": "SELECT COUNT(*) FROM items WHERE description LIKE '%token%'",
                    "expected_plan_features": ["all"],
                    "max_probe_sec": 1,
                }
            ],
            "scale_strategy": {
                "initial_rows": 1000,
                "max_rows": 10000,
                "growth_factor": 2,
                "max_rounds": 2,
            },
        },
        "workload_spec": {
            "enabled": False,
            "method": "none",
            "queries": [],
            "concurrency": 1,
            "duration_sec": 1,
        },
        "evaluation_spec": {
            "validation_criteria": ["EXPLAIN uses ALL"],
            "symptom_evidence": ["latency increase"],
            "mechanism_evidence": ["full scan"],
            "minimum_plan_similarity": 0.6,
        },
        "experiment_request": {
            "target_anomaly": "post_reproduction",
            "target_database": "input_analysis_repro",
            "dba_description": "unindexed query",
            "target_path": [],
            "injected_nodes": [],
            "max_duration_sec": 10,
            "max_retry_rounds": 2,
            "risk_level": risk,
            "safety_overrides": {},
            "workload": {"enabled": False},
        },
        "task_specs": [
            {
                "task_id": "post_slow_query",
                "task_type": "slow_sql",
                "actions": [
                    {
                        "kind": "raw_sql_workload",
                        "database": "input_analysis_repro",
                        "sql": "SELECT COUNT(*) FROM items WHERE description LIKE '%token%'",
                        "concurrency": 1,
                        "duration_sec": 1,
                    }
                ],
                "expected_metrics": {"rows_examined_ratio": ">2"},
                "success_criteria": {"rows_examined_ratio": ">2"},
                "risk_assessment": risk,
                "metadata": {"root_cause": "improper_sql"},
            }
        ],
        "dependencies": [],
        "risk_assessment": risk,
        "requires_domain_judgment": False,
        "unresolved_critical_questions": [],
        "rationale": "Use a bounded full scan to reproduce the mechanism.",
    }


class ReproductionSchemaTests(unittest.TestCase):
    def test_canonicalizes_transaction_steps_and_calibration_explain(self):
        payload = blueprint_payload()
        payload["data_spec"]["generation_sql"] = ["-- prose", "INSERT INTO items VALUES (1, 'x')"]
        payload["data_spec"]["calibration_queries"][0]["sql"] = "EXPLAIN SELECT * FROM items"
        payload["task_specs"][0]["actions"] = [
            {
                "kind": "raw_transaction_script",
                "database": "input_analysis_repro",
                "duration_sec": 1,
                "steps": [{"sql": "SELECT 1"}],
            }
        ]
        normalized, changes = canonicalize_blueprint_payload(payload)
        action = normalized["task_specs"][0]["actions"][0]
        self.assertNotIn("steps", action)
        self.assertEqual(action["scripts"][0]["steps"], [{"sql": "SELECT 1"}])
        self.assertEqual(normalized["data_spec"]["calibration_queries"][0]["sql"], "SELECT * FROM items")
        self.assertEqual(normalized["data_spec"]["generation_sql"], ["INSERT INTO items VALUES (1, 'x')"])
        self.assertTrue(changes)

    def test_strong_blueprint_schema_accepts_valid_payload(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload())
        self.assertEqual(blueprint.feasibility.level, "mechanism")
        self.assertEqual(blueprint.incident_spec.facts[0].source, "explicit_post")

    def test_strong_blueprint_schema_rejects_untraceable_facts(self):
        payload = blueprint_payload()
        payload["incident_spec"]["facts"] = []
        with self.assertRaisesRegex(ValueError, "at least one"):
            ReproductionBlueprint.from_dict(payload)

    def test_strong_blueprint_schema_rejects_sql_array_with_clear_path(self):
        payload = blueprint_payload()
        payload["task_specs"][0]["actions"][0]["sql"] = ["SELECT 1", "SELECT 2"]
        with self.assertRaisesRegex(ValueError, "sql must be one non-empty SQL string"):
            ReproductionBlueprint.from_dict(payload)

    def test_gate_policy_covers_confidence_risk_and_failures(self):
        payload = blueprint_payload(confidence=0.5, risk="high")
        reasons = gate_reasons(payload, failed_rounds=2)
        self.assertIn("incident_confidence_below_0.70", reasons)
        self.assertIn("high_risk", reasons)
        self.assertIn("two_failed_reproduction_rounds", reasons)

    def test_controlled_patch_rejects_evidence_mutation(self):
        with self.assertRaisesRegex(ValueError, "non-editable"):
            apply_controlled_patch(blueprint_payload(), {"incident_spec": {"facts": []}})

    def test_controlled_patch_allows_data_constraints(self):
        updated = apply_controlled_patch(
            blueprint_payload(),
            {"data_spec": {"constraints": {"predicate_selectivity": {"description": 0.0001}}}},
        )
        self.assertEqual(updated["data_spec"]["constraints"]["predicate_selectivity"]["description"], 0.0001)


class ReproductionReactTests(unittest.TestCase):
    def test_native_tool_calling_returns_strong_blueprint(self):
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(blueprint_payload())}}]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(response).encode()

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            blueprint, trace = plan_reproduction(
                "LIKE query is slow",
                config=RuntimeConfig(openai_api_key="test", planner_enabled=True),
            )
        self.assertEqual(blueprint.incident_spec.dbms, "mysql")
        self.assertEqual(trace[-1]["tool"], "final_answer")

    def test_native_tool_calling_retries_timeout_and_preserves_trace(self):
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(blueprint_payload())}}]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(response).encode()

        with (
            patch("InputAnalysisAgent.react.time.sleep"),
            patch("urllib.request.urlopen", side_effect=[TimeoutError("read timed out"), FakeResponse()]),
        ):
            _blueprint, trace = plan_reproduction(
                "LIKE query is slow",
                config=RuntimeConfig(
                    openai_api_key="test",
                    planner_enabled=True,
                    input_analysis_llm_max_attempts=2,
                ),
            )
        self.assertEqual(trace[0]["event"], "request_timeout")
        self.assertEqual(trace[-1]["tool"], "final_answer")


class HumanLoopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = RuntimeConfig(openai_api_key="test", planner_enabled=True)

    def test_checkpoint_writes_gate_and_resumable_state(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload(confidence=0.5))
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = ReproductionRuntime(self.config)
            with patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(blueprint, [])):
                with self.assertRaises(HumanGateRequired) as caught:
                    runtime.run("uncertain post", output_root=tempdir, interaction="checkpoint")
            run_dir = caught.exception.run_dir
            self.assertTrue((run_dir / "hitl_request.json").exists())
            state = load_state(run_dir)
            self.assertEqual(state.status, "waiting_human")
            self.assertIn("planning", state.completed_phases)

    def test_planner_failure_writes_trace_and_retry_command(self):
        error = ReproductionPlanningError(
            "tool-calling request timed out",
            [{"step": 3, "event": "request_timeout", "attempt": 2}],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = ReproductionRuntime(self.config)
            with patch("InputAnalysisAgent.runtime.plan_reproduction", side_effect=error):
                with self.assertRaises(ReproductionPlanningError):
                    runtime.run("post", output_root=tempdir, interaction="checkpoint")
            run_dir = next(Path(tempdir).iterdir())
            failure = json.loads((run_dir / "planner_failure.json").read_text())
            self.assertIn("--decision retry", failure["retry_command"])
            self.assertEqual(failure["trace"][0]["step"], 3)
            self.assertEqual(load_state(run_dir).status, "failed")

    def test_failed_planning_run_can_retry_in_same_directory(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload(confidence=0.5))
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "input.json", {"dba_description": "post", "metadata": {}})
            save_state(run_dir, RunState("r1", "failed", "planning", "checkpoint", last_error="timeout"))
            runtime = ReproductionRuntime(self.config)
            with patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(blueprint, [])):
                with self.assertRaises(HumanGateRequired):
                    runtime.resume(run_dir, HumanDecision("retry"))
            state = load_state(run_dir)
            self.assertEqual(state.status, "waiting_human")
            self.assertIn("planning", state.completed_phases)

    def test_retry_reuses_canonicalized_candidate_without_llm(self):
        payload = blueprint_payload(confidence=0.5)
        payload["task_specs"][0]["actions"] = [
            {
                "kind": "raw_transaction_script",
                "database": "input_analysis_repro",
                "duration_sec": 1,
                "steps": [{"sql": "SELECT 1"}],
            }
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "input.json", {"dba_description": "post", "metadata": {}})
            write_json(run_dir / "candidate_blueprint.json", payload)
            write_json(run_dir / "react_trace.json", [])
            save_state(run_dir, RunState("r1", "failed", "planning", "checkpoint", last_error="invalid shape"))
            runtime = ReproductionRuntime(self.config)
            with patch("InputAnalysisAgent.runtime.plan_reproduction") as planner:
                with self.assertRaises(HumanGateRequired):
                    runtime.resume(run_dir, HumanDecision("retry"))
            planner.assert_not_called()
            normalized = json.loads((run_dir / "blueprint.json").read_text())
            self.assertIn("scripts", normalized["task_specs"][0]["actions"][0])

    def test_reject_marks_run_without_execution(self):
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "blueprint.json", blueprint_payload(confidence=0.5))
            save_state(run_dir, RunState("r1", "waiting_human", "approval", "checkpoint"))
            result = ReproductionRuntime(self.config).resume(run_dir, HumanDecision("reject"))
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(load_state(run_dir).status, "rejected")

    def test_revise_revalidates_and_cannot_introduce_drop_table(self):
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "blueprint.json", blueprint_payload(confidence=0.5))
            save_state(run_dir, RunState("r1", "waiting_human", "approval", "checkpoint"))
            patch_value = {
                "data_spec": {"schema_sql": ["DROP TABLE items"]}
            }
            with self.assertRaisesRegex(ValueError, "non-editable"):
                ReproductionRuntime(self.config).resume(
                    run_dir,
                    HumanDecision("revise", patch=patch_value),
                )

    def test_approve_resumes_after_completed_planning(self):
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "blueprint.json", blueprint_payload())
            write_json(run_dir / "input.json", {"dba_description": "post", "metadata": {}})
            state = RunState("r1", "waiting_human", "approval", "checkpoint", completed_phases=["planning"])
            save_state(run_dir, state)
            runtime = ReproductionRuntime(self.config)
            with patch.object(runtime, "_continue", return_value={"status": "completed"}) as continued:
                result = runtime.resume(run_dir, HumanDecision("approve"))
            self.assertEqual(result["status"], "completed")
            self.assertTrue(continued.call_args.kwargs["human_approved"])
            self.assertIn("approval", load_state(run_dir).completed_phases)

    def test_cli_returns_two_when_checkpoint_waits_for_human(self):
        with tempfile.TemporaryDirectory() as tempdir:
            post = Path(tempdir) / "post.txt"
            post.write_text("uncertain incident")
            gate = HumanGateRequired(Path(tempdir) / "run", {"summary": "review"})
            with patch("InputAnalysisAgent.cli.ReproductionRuntime.run", side_effect=gate):
                rc = cli_main([
                    "run",
                    "--input",
                    str(post),
                    "--output-root",
                    tempdir,
                    "--interaction",
                    "checkpoint",
                ])
            self.assertEqual(rc, 2)

    def test_full_mock_reproduction_writes_phase_artifacts(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload())
        evaluation = ReproductionEvaluation(True, True, 0.9, True, "mechanism reproduced")
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = ReproductionRuntime(self.config)
            with (
                patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(blueprint, [{"tool": "final_answer"}])),
                patch.object(runtime, "_prepare_mysql", return_value={"statement_count": 1}),
                patch.object(runtime, "_calibrate", return_value=(blueprint, {"matched": True, "rounds": []})),
                patch.object(runtime, "_execute", return_value={"baseline": {}, "after": {}, "execution_trace": {}}),
                patch("InputAnalysisAgent.runtime.evaluate_reproduction", return_value=evaluation),
            ):
                result = runtime.run("reproducible post", output_root=tempdir, interaction="checkpoint")
            run_dir = Path(result["run_dir"])
            self.assertEqual(result["status"], "completed")
            self.assertTrue((run_dir / "data_spec.json").exists())
            self.assertTrue((run_dir / "generated_task_specs.json").exists())
            self.assertTrue((run_dir / "reproduction_report.json").exists())
            self.assertEqual(load_state(run_dir).phase, "completed")

    def test_two_calibration_failures_require_human(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload())
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = ReproductionRuntime(self.config)
            with (
                patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(blueprint, [])),
                patch.object(runtime, "_prepare_mysql", return_value={}),
                patch.object(runtime, "_calibrate", return_value=(blueprint, {"matched": False, "rounds": [{}, {}]})),
            ):
                with self.assertRaises(HumanGateRequired) as caught:
                    runtime.run("hard post", output_root=tempdir, interaction="checkpoint")
            request = json.loads((caught.exception.run_dir / "hitl_request.json").read_text())
            self.assertIn("two_failed_reproduction_rounds", request["reasons"])


if __name__ == "__main__":
    unittest.main()
