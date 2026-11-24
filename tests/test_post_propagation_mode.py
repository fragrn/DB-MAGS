import tempfile
import unittest
from pathlib import Path

from InputAnalysisAgent.codex_runtime import CodexRuntime
from InputAnalysisAgent.hitl import load_state, save_state, write_json
from InputAnalysisAgent.propagation_adapter import validate_spec, validate_injections, evaluate_observations


class PropagationModeTests(unittest.TestCase):
    def spec(self):
        return validate_spec({"graph": {"graph_id": "g", "nodes": [{"node_id": "root"}, {"node_id": "symptom"}],
            "edges": [{"source": "root", "target": "symptom", "mechanism": "test mechanism",
                       "provenance": "agent_inference", "confidence": 0.5}]},
            "active_injection_nodes": ["root"],
            "observation_spec": {"root": "observe root", "symptom": "observe symptom"}})

    def test_modes_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CodexRuntime()
            for mode in ("direct", "propagation"):
                run = runtime.create("post", tmp, reproduction_mode=mode, max_reflections=1)
                self.assertEqual(run["config"]["reproduction_mode"], mode)
                self.assertEqual(run["config"]["max_attempts"], 2)
            with self.assertRaises(ValueError):
                runtime.create("post", tmp, reproduction_mode="unknown")

    def test_graph_and_injection_validation(self):
        spec = self.spec()
        validate_injections({"task_specs": [{"metadata": {"root_cause": "root"}}]}, spec)
        with self.assertRaises(ValueError):
            validate_injections({"task_specs": [{"metadata": {"root_cause": "symptom"}}]}, spec)
        spec["graph"]["edges"].append({"source": "symptom", "target": "root", "mechanism": "cycle", "provenance": "test", "confidence": 1})
        with self.assertRaises(ValueError):
            validate_spec(spec)

    def test_runtime_graph_gate_and_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CodexRuntime()
            run = runtime.create("post", tmp, reproduction_mode="propagation", max_reflections=1)
            root, attempt = Path(run["run_dir"]), Path(run["attempt_dir"])
            state = load_state(attempt)
            state.completed_phases = ["planning", "approval", "preparation", "calibration"]
            state.phase = "calibration"
            save_state(attempt, state)
            write_json(attempt / "blueprint.json", {"task_specs": [{"metadata": {"root_cause": "root"}}]})
            self.assertEqual(runtime.advance(root)["next_action"], "submit_graph")
            with self.assertRaisesRegex(ValueError, "complete graph"):
                runtime.execute(root)
            runtime.submit_graph(root, self.spec())
            self.assertEqual(runtime.advance(root)["next_action"], "execute")
            with self.assertRaisesRegex(ValueError, "frozen"):
                runtime.submit_graph(root, self.spec())
            state = load_state(attempt)
            state.completed_phases.append("execution")
            save_state(attempt, state)
            self.assertEqual(runtime.advance(root)["next_action"], "submit_propagation_evaluation")
            runtime.submit_propagation_evaluation(root, {"observations": []})
            self.assertEqual(runtime.advance(root)["next_action"], "submit_evaluation")

    def test_observations_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp)
            (attempt / "execution.json").write_text('{}')
            entries = [{"observation": {"node_id": n, "hit": True, "timestamp_sec": t},
                        "evidence_references": ["execution.json"]} for n, t in [("root", 1), ("symptom", 2)]]
            self.assertTrue(evaluate_observations(self.spec(), {"observations": entries}, attempt)["success"])
            result = evaluate_observations(self.spec(), {"observations": entries[:1]}, attempt)
            self.assertFalse(result["success"])
            self.assertEqual(result["missing_evidence"], ["symptom"])
            entries[0]["evidence_references"] = ["../missing.json"]
            with self.assertRaises(ValueError):
                evaluate_observations(self.spec(), {"observations": entries}, attempt)
