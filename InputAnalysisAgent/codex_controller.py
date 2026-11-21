"""Runtime-owned attempts; external Codex supplies decisions, never loop control."""
import json
from pathlib import Path

from InputAnalysisAgent.codex_runtime import CodexRuntime
from InputAnalysisAgent.hitl import write_json
from InputAnalysisAgent.react import SYSTEM_PROMPT


class ExperimentController:
    def __init__(self, driver, runtime=None):
        self.driver = driver
        self.runtime = runtime or CodexRuntime()

    def run(self, post, output_root, max_reflections=2):
        if max_reflections < 0:
            raise ValueError("max_reflections must be nonnegative")
        run = self.runtime.create(post, output_root, max_attempts=max_reflections + 1)
        root = Path(run["run_dir"])
        previous = None
        records = []
        workdir = Path(__file__).resolve().parent.parent
        for index in range(max_reflections + 1):
            context = self.runtime.status(root)
            attempt = Path(context["attempt_dir"])
            def decide(stage, instruction, evidence):
                return self.driver.decide(instruction + "\n" + json.dumps(evidence, ensure_ascii=False),
                                          attempt / ("codex_" + stage), workdir)
            try:
                evidence = {"post":post, "metadata":{"target_database":context["config"]["target_database"]},
                            "previous_attempt":previous}
                if index:
                    reflection = decide("reflection", "Analyze previous failed/partial evidence. Return JSON with failure_analysis and recommended_changes. Do not invent evidence.", evidence)
                    write_json(attempt / "reflection_result.json", reflection)
                    evidence["reflection"] = reflection
                bp = decide("planning", SYSTEM_PROMPT + "\nRead InputAnalysisAgent schemas and adapters for actual capabilities. Do not call internal LLM APIs. Return the complete Blueprint.", evidence)
                self.runtime.submit_blueprint(root, bp)
                self.runtime.prepare(root)
                explain = self.runtime.explain(root)
                calibration = decide("calibration", "Return decision accept|weak_match|reject and reasoning based on actual EXPLAIN. Include concerns. No revised blueprint.", {"blueprint":bp,"explain":explain})
                self.runtime.submit_calibration(root, calibration)
                if calibration.get("decision") != "accept":
                    raise ValueError("Calibration did not accept: " + json.dumps(calibration))
                execution = self.runtime.execute(root)
                evaluation = decide("evaluation", "Evaluate only supplied actual evidence against blueprint criteria. Return symptom_hit, mechanism_hit, success (booleans), plan_similarity (0..1), reason, unmatched_conditions (array), evidence (object). Completion alone is not reproduction. Success requires both hits and minimum similarity.", {"blueprint":bp,"explain":explain,"execution":execution})
                self.runtime.submit_evaluation(root, evaluation)
                record = {"attempt":index+1,"status":"success" if evaluation["success"] else "partial", "evaluation":evaluation}
            except (TimeoutError, OSError) as exc:
                write_json(root / "controller_status.json", {"status":"interrupted","error":str(exc),"attempt":index+1})
                raise
            except Exception as exc:
                record = {"attempt":index+1,"status":"failed","error":str(exc)}
                write_json(attempt / "controller_failure.json", record)
            records.append(record)
            previous = {"result":record,"artifact_directory":str(attempt)}
            write_json(root / "controller_status.json", {"status":"running","max_reflections":max_reflections,"attempts":records})
            if record["status"] == "success":
                break
            if index < max_reflections:
                self.runtime.revise(root, "Unsuccessful attempt requires reflection and a revised Blueprint")
        result = {"run_dir":str(root),"status":records[-1]["status"],"max_reflections":max_reflections,
                  "reflections_used":len(records)-1,"stop_reason":"success" if records[-1]["status"]=="success" else "budget_exhausted","attempts":records}
        write_json(root / "controller_status.json", result)
        return result
