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
from InputAnalysisAgent.react import (
    CALIBRATION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    ReproductionPlanningError,
    _canonicalize_evaluation_payload,
    _tool_schemas,
    calibrate_reproduction,
    canonicalize_blueprint_payload,
    plan_reproduction,
)
from InputAnalysisAgent.prompt_examples import DATASPEC_FORMAT_REFERENCE
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
                    "objective": "Verify that the unindexed predicate produces a table scan.",
                    "expected_evidence": [
                        "The plan scans the items table.",
                        "No secondary index can satisfy the description predicate.",
                    ],
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
    def test_evaluation_evidence_container_shorthand_is_preserved(self):
        listed = _canonicalize_evaluation_payload({"evidence": ["latency increased"]})
        textual = _canonicalize_evaluation_payload({"evidence": "latency increased"})
        missing = _canonicalize_evaluation_payload({"evidence": None})
        structured = _canonicalize_evaluation_payload({"evidence": {"p95_ms": 342}})
        self.assertEqual(listed["evidence"], {"items": ["latency increased"]})
        self.assertEqual(textual["evidence"], {"summary": "latency increased"})
        self.assertEqual(missing["evidence"], {})
        self.assertEqual(structured["evidence"], {"p95_ms": 342})

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

    def test_strong_blueprint_schema_rejects_string_table_entries(self):
        payload = blueprint_payload()
        payload["data_spec"]["tables"] = ["items"]
        with self.assertRaisesRegex(ValueError, "tables and calibration_queries must contain objects"):
            ReproductionBlueprint.from_dict(payload)

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

    def test_raw_command_protocol_uses_command_and_rejects_argv_alias(self):
        payload = blueprint_payload()
        payload["task_specs"][0]["actions"] = [{
            "kind": "raw_command",
            "command": ["printf", "ok"],
            "duration_sec": 1,
        }]
        ReproductionBlueprint.from_dict(payload)

        payload["task_specs"][0]["actions"][0] = {
            "kind": "raw_command",
            "argv": ["printf", "ok"],
            "duration_sec": 1,
        }
        with self.assertRaisesRegex(ValueError, "command must be a non-empty argv string array"):
            ReproductionBlueprint.from_dict(payload)

    def test_system_prompt_names_command_field_and_forbids_argv_field(self):
        self.assertIn('the field name must be\n"command"', SYSTEM_PROMPT)
        self.assertIn('Never use "argv" as a field name', SYSTEM_PROMPT)
        self.assertIn('"command":["grep"', SYSTEM_PROMPT)

    def test_prompts_include_dataspec_format_reference(self):
        self.assertIn(DATASPEC_FORMAT_REFERENCE, SYSTEM_PROMPT)
        self.assertIn(DATASPEC_FORMAT_REFERENCE, CALIBRATION_SYSTEM_PROMPT)
        self.assertIn('"tables": [', DATASPEC_FORMAT_REFERENCE)
        self.assertIn('"name": "main_table"', DATASPEC_FORMAT_REFERENCE)
        self.assertIn('"purpose":', DATASPEC_FORMAT_REFERENCE)
        self.assertIn('"target_rows":', DATASPEC_FORMAT_REFERENCE)
        self.assertIn('"distribution_notes":', DATASPEC_FORMAT_REFERENCE)
        self.assertIn("data_spec.tables must be an array of objects", DATASPEC_FORMAT_REFERENCE)
        self.assertIn("never an array of strings", DATASPEC_FORMAT_REFERENCE)

    def test_strong_blueprint_schema_rejects_legacy_calibration_features(self):
        payload = blueprint_payload()
        query = payload["data_spec"]["calibration_queries"][0]
        query["conditions"] = [{"metric": "access_method"}]
        with self.assertRaisesRegex(ValueError, "unsupported rule-based fields"):
            ReproductionBlueprint.from_dict(payload)

    def test_calibration_requires_objective_and_expected_evidence(self):
        payload = blueprint_payload()
        payload["data_spec"]["calibration_queries"][0]["objective"] = ""
        with self.assertRaisesRegex(ValueError, "objective is required"):
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

    def test_calibration_approval_records_override_and_continues(self):
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "blueprint.json", blueprint_payload())
            write_json(run_dir / "input.json", {"dba_description": "post", "metadata": {}})
            write_json(run_dir / "calibration_result.json", {"status": "failed", "matched": False, "rounds": [{}, {}]})
            state = RunState(
                "r1",
                "waiting_human",
                "calibration",
                "checkpoint",
                completed_phases=["planning", "approval", "preparation"],
                calibration_failed_rounds=2,
            )
            save_state(run_dir, state)
            runtime = ReproductionRuntime(self.config)
            with patch.object(runtime, "_continue", return_value={"status": "completed"}) as continued:
                result = runtime.resume(run_dir, HumanDecision("approve"))
            self.assertEqual(result["status"], "completed")
            effective = json.loads((run_dir / "calibration_result.json").read_text())
            self.assertTrue(effective["matched"])
            self.assertFalse(effective["observed_matched"])
            self.assertTrue((run_dir / "calibration_override.json").exists())
            self.assertIn("calibration", load_state(run_dir).completed_phases)
            self.assertTrue(continued.call_args.kwargs["human_approved"])

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

    def test_calibration_weak_match_requires_human(self):
        blueprint = ReproductionBlueprint.from_dict(blueprint_payload())
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = ReproductionRuntime(self.config)
            with (
                patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(blueprint, [])),
                patch.object(runtime, "_prepare_mysql", return_value={}),
                patch.object(runtime, "_calibrate", return_value=(blueprint, {
                    "status": "weak_match",
                    "decision": "weak_match",
                    "matched": False,
                    "concerns": ["close but not exact"],
                    "recommended_changes": ["DBA may approve"],
                    "rounds": [{}],
                })),
            ):
                with self.assertRaises(HumanGateRequired) as caught:
                    runtime.run("hard post", output_root=tempdir, interaction="checkpoint")
            request = json.loads((caught.exception.run_dir / "hitl_request.json").read_text())
            self.assertIn("calibration_weak_match", request["reasons"])
            self.assertEqual(request["details"]["calibration"]["concerns"], ["close but not exact"])
            state = load_state(caught.exception.run_dir)
            self.assertEqual(state.calibration_failed_rounds, 1)
            self.assertEqual(state.evaluation_failed_rounds, 0)


class CalibrationReactTests(unittest.TestCase):
    def setUp(self):
        self.config = RuntimeConfig(openai_api_key="test", planner_enabled=True)
        self.blueprint = ReproductionBlueprint.from_dict(blueprint_payload())
        self.sql = self.blueprint.data_spec.calibration_queries[0]["sql"]

    def _accepted_payload(self) -> dict:
        return {
            "decision": "accept",
            "reasoning": "The observed EXPLAIN supports the intended scan mechanism.",
            "query_assessments": [{
                "sql": self.sql,
                "matched": True,
                "observed_plan_summary": "MySQL reports a table scan on items.",
                "supporting_evidence": ["The EXPLAIN row identifies a table scan."],
                "discrepancies": [],
            }],
            "concerns": [],
            "recommended_changes": [],
            "missing_information": [],
        }

    def _explain_trace(self) -> list[dict]:
        return [{
            "step": 1,
            "tool": "explain_sql",
            "arguments": {"database": "input_analysis_repro", "sql": self.sql},
            "result": {"plan": [{"EXPLAIN": "-> Table scan on items"}]},
        }]

    def test_calibration_tool_surface_only_exposes_explain(self):
        names = {item["function"]["name"] for item in _tool_schemas({"explain_sql"})}
        self.assertEqual(names, {"explain_sql"})

    def test_llm_accept_decision_is_accepted_without_rule_matching(self):
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": self._accepted_payload(),
            "trace": self._explain_trace(),
        }):
            result, revised, _trace = calibrate_reproduction(
                "post",
                self.blueprint,
                {"statement_count": 1},
                round_no=1,
                config=self.config,
            )
        self.assertEqual(result["decision"], "accept")
        self.assertIsNone(revised)

    def test_calibration_uses_query_id_with_harmless_sql_formatting(self):
        payload = self._accepted_payload()
        payload["query_assessments"][0]["query_id"] = "calibration_query_1"
        payload["query_assessments"][0]["sql"] = f"  {self.sql};  "
        trace = self._explain_trace()
        trace[0]["arguments"] = {
            "database": "input_analysis_repro",
            "query_id": "calibration_query_1",
            "sql": f"  {self.sql};  ",
        }
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": payload,
            "trace": trace,
        }):
            result, revised, _trace = calibrate_reproduction(
                "post",
                self.blueprint,
                {},
                round_no=1,
                config=self.config,
            )
        self.assertEqual(result["decision"], "accept")
        self.assertIsNone(revised)

    def test_calibration_coverage_normalizes_explain_prefix(self):
        hinted_sql = "SELECT /*+ SET_VAR(optimizer_switch='semijoin=off') */ * FROM items"
        payload = blueprint_payload()
        payload["data_spec"]["calibration_queries"][0]["sql"] = hinted_sql
        blueprint = ReproductionBlueprint.from_dict(payload)
        result_payload = self._accepted_payload()
        result_payload["query_assessments"][0]["sql"] = hinted_sql
        trace = self._explain_trace()
        trace[0]["arguments"]["sql"] = f"EXPLAIN {hinted_sql}"
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": result_payload,
            "trace": trace,
        }):
            result, _revised, _trace = calibrate_reproduction(
                "post",
                blueprint,
                {},
                round_no=1,
                config=self.config,
            )
        self.assertEqual(result["decision"], "accept")

    def test_weak_match_is_valid_without_revised_blueprint(self):
        result_payload = self._accepted_payload()
        result_payload.update({
            "decision": "weak_match",
            "reasoning": "The plan differs but still shows the target mechanism.",
            "concerns": ["The join order differs from the post."],
            "recommended_changes": ["DBA may force the original join order."],
        })
        result_payload["query_assessments"][0]["matched"] = False
        result_payload["query_assessments"][0]["discrepancies"] = ["Join order differs."]
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": result_payload,
            "trace": self._explain_trace(),
        }):
            result, revised, trace = calibrate_reproduction(
                "post",
                self.blueprint,
                {},
                round_no=1,
                config=self.config,
            )
        self.assertEqual(result["decision"], "weak_match")
        self.assertIsNone(revised)
        self.assertEqual(trace, self._explain_trace())

    def test_reject_is_valid_without_revised_blueprint(self):
        result_payload = self._accepted_payload()
        result_payload.update({
            "decision": "reject",
            "reasoning": "The plan is unrelated to the target mechanism.",
            "concerns": ["The target table is not present in the plan."],
            "recommended_changes": ["Revise the schema or query."],
        })
        result_payload["query_assessments"][0]["matched"] = False
        result_payload["query_assessments"][0]["discrepancies"] = ["No target mechanism observed."]
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": result_payload,
            "trace": self._explain_trace(),
        }):
            result, revised, _trace = calibrate_reproduction(
                "post",
                self.blueprint,
                {},
                round_no=1,
                config=self.config,
            )
        self.assertEqual(result["decision"], "reject")
        self.assertIsNone(revised)

    def test_accept_without_explain_call_is_rejected(self):
        with patch("InputAnalysisAgent.react._tool_loop", return_value={
            "json_payload": self._accepted_payload(),
            "trace": [{"step": 1, "tool": "final_answer"}],
        }):
            with self.assertRaisesRegex(ReproductionPlanningError, "must call explain_sql"):
                calibrate_reproduction(
                    "post",
                    self.blueprint,
                    {},
                    round_no=1,
                    config=self.config,
                )

    def test_runtime_accepts_calibration_without_reprepare(self):
        accepted = self._accepted_payload()
        runtime = ReproductionRuntime(self.config)
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "preparation_result.json", {"statement_count": 1})
            state = RunState("r1", "running", "calibration", "checkpoint")
            with (
                patch("InputAnalysisAgent.runtime.calibrate_reproduction", return_value=(accepted, None, self._explain_trace())),
                patch.object(runtime, "_prepare_mysql", return_value={"statement_count": 2}) as prepare,
            ):
                _blueprint, result = runtime._calibrate(
                    run_dir,
                    state,
                    {"dba_description": "post", "metadata": {}},
                    self.blueprint,
                )
            self.assertTrue(result["matched"])
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(result["rounds"]), 1)
            prepare.assert_not_called()
            self.assertTrue((run_dir / "react_trace_calibration_round_1.json").exists())

    def test_runtime_weak_match_requires_human_with_details(self):
        weak = self._accepted_payload()
        weak.update({
            "decision": "weak_match",
            "reasoning": "The plan is close enough to execute but not exact.",
            "concerns": ["Join order differs."],
            "recommended_changes": ["DBA may approve or force join order."],
        })
        weak["query_assessments"][0]["matched"] = False
        runtime = ReproductionRuntime(self.config)
        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(self.blueprint, [])),
                patch.object(runtime, "_prepare_mysql", return_value={}),
                patch("InputAnalysisAgent.runtime.calibrate_reproduction", return_value=(weak, None, self._explain_trace())),
            ):
                with self.assertRaises(HumanGateRequired) as caught:
                    runtime.run("post", output_root=tempdir, interaction="checkpoint")
            request = json.loads((caught.exception.run_dir / "hitl_request.json").read_text())
            self.assertIn("calibration_weak_match", request["reasons"])
            self.assertEqual(request["details"]["calibration"]["concerns"], ["Join order differs."])

    def test_runtime_reject_requires_human_with_details(self):
        rejected = self._accepted_payload()
        rejected.update({
            "decision": "reject",
            "reasoning": "The plan does not show the target mechanism.",
            "concerns": ["Target table is absent."],
            "recommended_changes": ["Revise the query."],
        })
        rejected["query_assessments"][0]["matched"] = False
        runtime = ReproductionRuntime(self.config)
        with tempfile.TemporaryDirectory() as tempdir:
            run_dir = Path(tempdir)
            write_json(run_dir / "preparation_result.json", {})
            state = RunState("r1", "running", "calibration", "checkpoint")
            with patch("InputAnalysisAgent.runtime.calibrate_reproduction", return_value=(rejected, None, self._explain_trace())):
                _blueprint, result = runtime._calibrate(
                    run_dir,
                    state,
                    {"dba_description": "post", "metadata": {}},
                    self.blueprint,
                )
            self.assertEqual(result["status"], "rejected_by_llm")
            self.assertFalse(result["matched"])
            self.assertEqual(state.calibration_failed_rounds, 1)

    def test_no_calibration_queries_is_explicitly_not_applicable(self):
        payload = blueprint_payload()
        payload["data_spec"]["calibration_queries"] = []
        blueprint = ReproductionBlueprint.from_dict(payload)
        runtime = ReproductionRuntime(RuntimeConfig())
        with tempfile.TemporaryDirectory() as tempdir:
            state = RunState("r1", "running", "calibration", "checkpoint")
            _blueprint, result = runtime._calibrate(
                Path(tempdir),
                state,
                {"dba_description": "post", "metadata": {}},
                blueprint,
            )
        self.assertEqual(result["status"], "not_applicable")
        self.assertTrue(result["matched"])


if __name__ == "__main__":
    unittest.main()
