import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from InputAnalysisAgent.codex_runtime import CodexRuntime
from InputAnalysisAgent.evaluation import evaluate
from InputAnalysisAgent.evidence import capture_bundle, collect
from InputAnalysisAgent.hitl import write_json
from InputAnalysisAgent.react import judge_evidence
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint
from agent.config import RuntimeConfig
from tests.test_input_analysis_reproduction import blueprint_payload


def criterion(id_, dimension, kind, inputs, **details):
    return dict(id=id_, dimension=dimension, kind=kind, inputs=inputs, required=True,
                description=id_, post_basis="The post describes this behavior", **details)


def contract():
    return {"version": 2, "criteria": [
        criterion("latency", "symptom", "numeric", {
            "target": "/after/p95", "control": "/baseline/p95",
        }, operation="ratio", operator=">=", threshold=2, threshold_source="Experiment definition"),
        criterion("lock", "mechanism", "semantic", {"lock_samples": "/execution_actions/query/0/wait"}),
        criterion("ready", "environment", "event", {"target": "/preparation_result/database"},
                  expected="test_db"),
    ]}


def execution():
    return {"baseline": {"p95": 2}, "after": {"p95": 6}, "execution_trace": {"tasks": {
        "query": {"actions": [{"wait": {"type": "row", "table": "items"}}], "errors": []},
    }}}


def judgments(index):
    refs = [eid for eid, item in index.items() if item["criterion_id"] == "lock" and item["available"]]
    return {"lock": {"status": "hit", "reason": "Observed row lock on the target table", "evidence_ids": refs}}


class EvidenceEvaluationTests(unittest.TestCase):
    def test_missing_evidence_and_zero_baseline_are_not_hits(self):
        spec = contract()
        bundle = {"baseline": {"p95": 0}, "after": {"p95": 6},
                  "preparation_result": {"database": "test_db"}}
        result = evaluate(spec, bundle, {"lock": {"status": "hit", "reason": "guess", "evidence_ids": []}},
                          attempt_id="a").to_dict()
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["dimensions"], {"symptom": "unknown", "mechanism": "unknown", "environment": "hit"})
        self.assertNotIn("plan_similarity", result)

    def test_rule_miss_with_positive_mechanism_is_partial(self):
        spec = contract()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = capture_bundle(Path(tmp), execution())
        bundle["after"]["p95"] = 2
        bundle["preparation_result"] = {"database": "test_db"}
        result = evaluate(spec, bundle, judgments(collect(spec, bundle, attempt_id="a")), attempt_id="a").to_dict()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["dimensions"]["symptom"], "miss")

    def test_cross_attempt_and_other_criterion_ids_are_rejected(self):
        spec = contract()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = capture_bundle(Path(tmp), execution())
        index = collect(spec, bundle, attempt_id="a")
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            evaluate(spec, bundle, judgments(index), attempt_id="b")
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            evaluate(spec, bundle, {"lock": {"status": "hit", "reason": "wrong input",
                                           "evidence_ids": [next(iter(index))]}}, attempt_id="a")

    def test_external_driver_freezes_and_verifies_current_attempt(self):
        runtime = ReproductionRuntime()
        driver = CodexRuntime(runtime)
        with tempfile.TemporaryDirectory() as tmp:
            run = driver.create("An update waits on a row lock.", tmp)
            root, attempt = Path(run["run_dir"]), Path(run["attempt_dir"])
            database = run["config"]["target_database"]
            bp = blueprint_payload()
            bp["evaluation_spec"] = contract()
            for section in ("data_spec", "environment_spec"):
                bp[section]["database"] = database
            bp["data_spec"]["calibration_queries"] = []
            bp["experiment_request"]["target_database"] = database
            bp["task_specs"][0]["actions"][0]["database"] = database
            with patch.object(runtime, "_validate_blueprint_safety"):
                driver.submit_blueprint(root, bp)
            with patch.object(runtime, "_prepare_database", return_value={"database": "test_db"}):
                driver.prepare(root)
            driver.explain(root)
            driver.submit_calibration(root, {"decision": "accept", "reasoning": "No calibration query"})
            with patch.object(runtime, "_execute", return_value=execution()):
                driver.execute(root)
            index = driver.evaluation_evidence(root)
            with self.assertRaisesRegex(ValueError, "only semantic"):
                driver.submit_evaluation(root, {"success": True})
            stored = runtime._read_json(attempt / "blueprint.json")
            changed = copy.deepcopy(stored)
            changed["evaluation_spec"]["criteria"][0]["threshold"] = 1
            write_json(attempt / "blueprint.json", changed)
            with self.assertRaisesRegex(ValueError, "criteria changed"):
                driver.submit_evaluation(root, {})
            write_json(attempt / "blueprint.json", stored)
            bundle_path = attempt / "execution_bundle.json"
            bundle = runtime._read_json(bundle_path)
            altered = copy.deepcopy(bundle)
            altered["after"]["p95"] = 999
            write_json(bundle_path, altered)
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                driver.submit_evaluation(root, {})
            write_json(bundle_path, bundle)
            self.assertTrue(driver.submit_evaluation(root, {"judgments": judgments(index)})["evaluation"]["success"])
            with self.assertRaisesRegex(ValueError, "already completed"):
                driver.submit_evaluation(root, {})

    def test_internal_flow_uses_same_evaluator(self):
        runtime = ReproductionRuntime()
        bp = blueprint_payload()
        bp["evaluation_spec"] = contract()
        bp["data_spec"]["calibration_queries"] = []
        with tempfile.TemporaryDirectory() as tmp, (
            patch("InputAnalysisAgent.runtime.plan_reproduction", return_value=(ReproductionBlueprint.from_dict(bp), []))
        ), patch.object(runtime, "_validate_blueprint_safety"), (
            patch.object(runtime, "_prepare_database", return_value={"database": "test_db"})
        ), patch.object(runtime, "_execute", return_value=execution()), (
            patch("InputAnalysisAgent.runtime.judge_evidence", side_effect=lambda spec, evidence, **_: judgments(evidence))
        ):
            result = runtime.run("An update waits on a row lock.", output_root=tmp)
            self.assertTrue(result["evaluation"]["success"])
            self.assertEqual(len(list(Path(result["run_dir"]).glob("evaluation_attempt_*/evaluation_result.json"))), 1)

    def test_model_receives_only_available_semantic_evidence(self):
        spec = contract()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = capture_bundle(Path(tmp), execution())
        index = collect(spec, bundle, attempt_id="a")
        config = RuntimeConfig(openai_model="gpt-5", openai_api_key="test")
        reply = {"choices": [{"message": {"content": json.dumps({"judgments": judgments(index)})}}]}
        with patch("InputAnalysisAgent.react._request_with_retry", return_value=(reply, "")) as request:
            self.assertEqual(judge_evidence(spec, index, post="post", config=config), judgments(index))
            body = json.loads(request.call_args.args[0].data)
            context = json.loads(body["messages"][1]["content"])
            self.assertEqual([c["id"] for c in context["criteria"]], ["lock"])
            self.assertTrue(all(item["criterion_id"] == "lock" for item in context["evidence"].values()))
            self.assertNotIn("temperature", body)
            request.reset_mock()
            self.assertEqual(judge_evidence(spec, collect(spec, {}, attempt_id="b"), post="post", config=config), {})
            request.assert_not_called()
