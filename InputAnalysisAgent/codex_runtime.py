"""External Codex decisions over the existing reproduction phase executors."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from InputAnalysisAgent.hitl import RunState, gate_reasons, load_state, save_state, write_json
from InputAnalysisAgent.react import _call_tool
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint, ReproductionEvaluation


class CodexRuntime:
    """One post, explicit phase commands, and no internal model calls."""

    def __init__(self, runtime: ReproductionRuntime | None = None):
        self.runtime = runtime or ReproductionRuntime()

    def create(self, post: str, output_root: str, *, hitl_enabled: bool = False,
               review_enabled: bool = False, max_attempts: int = 4) -> dict[str, Any]:
        if not post.strip() or max_attempts < 1:
            raise ValueError("non-empty post and positive max_attempts required")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:8]
        root = Path(output_root).resolve() / run_id
        root.mkdir(parents=True, exist_ok=False)
        write_json(root / "input.json", {"dba_description": post})
        write_json(root / "codex_config.json", {
            "driver": "codex", "hitl_enabled": hitl_enabled,
            "review_enabled": review_enabled, "max_attempts": max_attempts,
            "target_database": "repro_" + run_id.replace("-", ""),
        })
        self._new_attempt(root, 1)
        return self.status(root)

    def _new_attempt(self, root: Path, number: int) -> None:
        attempt = root / f"attempt_codex_{number}"
        attempt.mkdir(exist_ok=False)
        save_state(attempt, RunState(run_id=root.name, status="running", phase="planning",
                                    interaction="checkpoint"))
        write_json(root / "current_attempt.json", {"number": number, "directory": attempt.name})

    def _context(self, root: str | Path):
        root = Path(root).resolve()
        config = self.runtime._read_json(root / "codex_config.json")
        current = self.runtime._read_json(root / "current_attempt.json")
        attempt = root / current["directory"]
        return root, config, current, attempt, load_state(attempt)

    def _event(self, root: Path, command: str, **details: Any) -> None:
        with (root / "decision_history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": time.time(), "actor": "codex",
                                     "command": command, **details}, ensure_ascii=False) + "\n")

    def status(self, root: str | Path) -> dict[str, Any]:
        root, config, current, attempt, state = self._context(root)
        return {"run_dir": str(root), "attempt_dir": str(attempt), "attempt": current["number"],
                "config": config, "state": state.to_dict()}

    def submit_blueprint(self, root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
        root, config, _, attempt, state = self._context(root)
        if "planning" in state.completed_phases:
            raise ValueError("blueprint already submitted; use revise to create a new attempt")
        blueprint = ReproductionBlueprint.from_dict(payload)
        if blueprint.data_spec.database != config["target_database"]:
            raise ValueError(f"use assigned database {config['target_database']}")
        if blueprint.experiment_request["target_database"] != blueprint.data_spec.database:
            raise ValueError("experiment_request database must match data_spec")
        for task in blueprint.task_specs:
            for action in task.get("actions", []) + task.get("cleanup_actions", []):
                if action.get("database", blueprint.data_spec.database) != blueprint.data_spec.database:
                    raise ValueError("all actions must use the assigned database")
                if action["kind"] == "benchbase_burst_command":
                    raise ValueError("benchbase_burst_command is not implemented by this executor")
        self.runtime._validate_blueprint_safety(blueprint)
        self.runtime._write_blueprint_artifacts(attempt, blueprint)
        self.runtime._complete_phase(attempt, state, "planning")
        state.phase = "approval"
        reasons = gate_reasons(blueprint.to_dict())
        write_json(attempt / "approval.json", {
            "human_approved": False, "review_approved": False,
            "hitl_enabled": config["hitl_enabled"], "review_enabled": config["review_enabled"],
            "reasons": reasons,
        })
        save_state(attempt, state)
        self._event(root, "submit-blueprint", hitl_skipped=not config["hitl_enabled"],
                    review_skipped=not config["review_enabled"])
        return self.status(root)

    def approve(self, root: str | Path, reason: str, *, review: bool = False) -> dict[str, Any]:
        root, _, _, attempt, state = self._context(root)
        if not reason.strip() or "planning" not in state.completed_phases:
            raise ValueError("submitted blueprint and non-empty reason required")
        if state.phase not in {"approval", "calibration"}:
            raise ValueError("approval is only allowed at approval or calibration")
        approval = self.runtime._read_json(attempt / "approval.json")
        approval["review_approved" if review else "human_approved"] = True
        write_json(attempt / "approval.json", approval)
        self._event(root, "review" if review else "approve", reason=reason, phase=state.phase,
                    actor_source="reviewer" if review else "human")
        if state.phase == "calibration" and not review:
            write_json(attempt / "calibration_override.json", {"reason": reason, "actor": "human"})
            self.runtime._complete_phase(attempt, state, "calibration")
        return self.status(root)

    def _require(self, state: RunState, phase: str) -> None:
        if phase not in state.completed_phases:
            raise ValueError(f"complete {phase} first")

    def prepare(self, root: str | Path) -> dict[str, Any]:
        root, config, _, attempt, state = self._context(root)
        self._require(state, "planning")
        if "preparation" in state.completed_phases:
            return self.runtime._read_json(attempt / "preparation_result.json")
        approval = self.runtime._read_json(attempt / "approval.json")
        if config["hitl_enabled"] and not approval["human_approved"]:
            raise ValueError("human approval required: use codex approve")
        if config["review_enabled"] and not approval["review_approved"]:
            raise ValueError("independent review required: use codex review")
        self.runtime._complete_phase(attempt, state, "approval")
        return self._perform(root, "preparation", "preparation_result.json",
                             lambda bp: self.runtime._prepare_database(bp))

    def _perform(self, root: Path, phase: str, filename: str, operation) -> dict[str, Any]:
        _, _, _, attempt, state = self._context(root)
        state.phase = phase
        save_state(attempt, state)
        try:
            bp = ReproductionBlueprint.from_dict(self.runtime._read_json(attempt / "blueprint.json"))
            result = operation(bp)
            write_json(attempt / filename, result)
            self.runtime._complete_phase(attempt, state, phase)
            self._event(root, phase, status="completed")
            return result
        except Exception as exc:
            state.status, state.last_error = "failed", str(exc)
            save_state(attempt, state)
            write_json(attempt / "failure.json", {"phase": phase, "error": str(exc)})
            self._event(root, phase, status="failed", error=str(exc))
            raise

    def inspect(self, root: str | Path, *, capabilities: bool = False) -> dict[str, Any]:
        _, config, _, attempt, _ = self._context(root)
        bp_path = attempt / "blueprint.json"
        bp = self.runtime._read_json(bp_path) if bp_path.exists() else {}
        arguments = {} if capabilities else {
            "database": config["target_database"],
            "dbms": (bp.get("incident_spec") or {}).get("dbms", "mysql"),
        }
        result = _call_tool("inspect_capabilities" if capabilities else "inspect_db_environment",
                            arguments, self.runtime.config)
        write_json(attempt / ("capabilities.json" if capabilities else "inspection.json"), result)
        return result

    def explain(self, root: str | Path) -> dict[str, Any]:
        _, _, _, attempt, state = self._context(root)
        self._require(state, "preparation")
        bp = ReproductionBlueprint.from_dict(self.runtime._read_json(attempt / "blueprint.json"))
        results = []
        for query in bp.data_spec.calibration_queries:
            try:
                evidence = _call_tool("explain_sql", {"database": bp.data_spec.database,
                    "dbms": bp.incident_spec.dbms, "sql": query["sql"]}, self.runtime.config)
                results.append({"sql": query["sql"], "evidence": evidence})
            except Exception as exc:
                results.append({"sql": query["sql"], "error": str(exc)})
        result = {"queries": results}
        write_json(attempt / "explain_result.json", result)
        state.phase = "calibration"
        save_state(attempt, state)
        return result

    def submit_calibration(self, root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
        root, config, _, attempt, state = self._context(root)
        self._require(state, "preparation")
        decision = payload.get("decision")
        if decision not in {"accept", "weak_match", "reject"} or not str(payload.get("reasoning", "")).strip():
            raise ValueError("decision accept/weak_match/reject and reasoning required")
        bp = self.runtime._read_json(attempt / "blueprint.json")
        if bp["data_spec"].get("calibration_queries") and not (attempt / "explain_result.json").exists():
            raise ValueError("run explain before submitting calibration")
        continued = decision == "accept" or (not config["hitl_enabled"] and payload.get("continue_execution") is True)
        write_json(attempt / "calibration_result.json", {**payload, "matched": continued,
                   "status": "accepted" if decision == "accept" else decision, "actor": "codex"})
        if continued:
            self.runtime._complete_phase(attempt, state, "calibration")
        else:
            state.phase = "calibration"
            state.status = "waiting_human" if config["hitl_enabled"] else "running"
            save_state(attempt, state)
        self._event(root, "submit-calibration", **payload)
        return self.status(root)

    def execute(self, root: str | Path) -> dict[str, Any]:
        root, _, _, attempt, state = self._context(root)
        self._require(state, "calibration")
        if state.status == "failed":
            raise ValueError("failed attempt cannot execute again; use revise")
        if "execution" in state.completed_phases:
            return self.runtime._read_json(attempt / "execution_bundle.json")
        return self._perform(root, "execution", "execution_bundle.json",
                             lambda bp: self.runtime._execute(bp, attempt))

    def submit_evaluation(self, root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
        root, _, _, attempt, state = self._context(root)
        self._require(state, "execution")
        for field in ("success", "symptom_hit", "mechanism_hit"):
            if not isinstance(payload.get(field), bool):
                raise ValueError(f"{field} must be boolean")
        evaluation = ReproductionEvaluation.from_dict(payload)
        bp = self.runtime._read_json(attempt / "blueprint.json")
        minimum = float(bp["evaluation_spec"].get("minimum_plan_similarity", 0.6))
        if evaluation.success and not (evaluation.symptom_hit and evaluation.mechanism_hit
                                       and evaluation.plan_similarity >= minimum):
            raise ValueError("success requires symptom, mechanism, and similarity threshold")
        if not evaluation.reason.strip() or not evaluation.evidence:
            raise ValueError("reason and evidence required")
        write_json(attempt / "evaluation_result.json", evaluation.to_dict())
        self.runtime._complete_phase(attempt, state, "evaluation")
        state.phase = "completed"
        state.status = "completed"
        save_state(attempt, state)
        report = {"driver": "codex", "evaluation": evaluation.to_dict(), "attempt_dir": str(attempt)}
        write_json(attempt / "reproduction_report.json", report)
        self._event(root, "submit-evaluation", success=evaluation.success)
        return report

    def revise(self, root: str | Path, reason: str) -> dict[str, Any]:
        root, config, current, attempt, _ = self._context(root)
        if not reason.strip():
            raise ValueError("revision reason required")
        if current["number"] >= config["max_attempts"]:
            raise ValueError("max_attempts reached; no more attempts allowed")
        self._new_attempt(root, current["number"] + 1)
        _, _, _, next_attempt, _ = self._context(root)
        write_json(next_attempt / "revision.json", {"previous_attempt": str(attempt), "reason": reason})
        self._event(root, "revise", reason=reason)
        return self.status(root)
