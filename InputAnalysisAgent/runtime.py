"""Resumable runtime for reproducing anomalies described by DBA posts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agent import tools as agent_tools
from agent.config import RuntimeConfig

from InputAnalysisAgent.hitl import (
    HumanDecision,
    HumanGateRequired,
    RunState,
    apply_controlled_patch,
    gate_reasons,
    load_state,
    record_decision,
    save_state,
    write_gate,
    write_json,
)
from InputAnalysisAgent.db_adapters import PostgresAdapter, SqlServerAdapter, normalize_dbms, supported_execution_dbms
from InputAnalysisAgent.react import (
    ReproductionPlanningError,
    calibrate_reproduction,
    canonicalize_blueprint_payload,
    evaluate_reproduction,
    plan_reproduction,
)
from InputAnalysisAgent.schemas import ReproductionBlueprint
from InputAnalysisAgent.schemas import ReproductionEvaluation
from InputAnalysisAgent.slowlog import SlowLogProbe, evaluate_slow_log_evidence, is_slow_log_reproduction


PHASES = ("planning", "approval", "preparation", "calibration", "execution", "evaluation", "completed")
_UNSAFE_SETUP_SQL = (
    re.compile(r"\bDROP\s+(DATABASE|TABLE)\b", re.I),
    re.compile(r"\bTRUNCATE\b", re.I),
    re.compile(r"\bLOAD\s+DATA\s+(LOCAL\s+)?INFILE\b", re.I),
    re.compile(r"\bSET\s+GLOBAL\b", re.I),
    re.compile(r"\bSHUTDOWN\b", re.I),
)


class ReproductionRuntime:
    def __init__(self, config: RuntimeConfig | None = None):
        self.config = config or RuntimeConfig.from_env()

    def run(
        self,
        post: str,
        *,
        metadata: dict[str, Any] | None = None,
        output_root: str = "InputAnalysisExperiment_runs",
        interaction: str = "checkpoint",
    ) -> dict[str, Any]:
        if interaction not in {"interactive", "checkpoint"}:
            raise ValueError("interaction must be interactive or checkpoint")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:8]
        run_dir = Path(output_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        write_json(run_dir / "input.json", {"dba_description": post, "metadata": metadata or {}})
        state = RunState(run_id=run_id, status="running", phase="planning", interaction=interaction)
        save_state(run_dir, state)
        return self._continue(run_dir, state)

    def resume(self, run_dir: str | Path, decision: HumanDecision) -> dict[str, Any]:
        run_dir = Path(run_dir)
        state = load_state(run_dir)
        gate_phase = state.phase
        decision.validate()
        if decision.decision == "retry":
            if state.status != "failed":
                raise ValueError(f"retry requires a failed run: status={state.status}")
            state.status = "running"
            state.last_error = ""
            state.pending_gate = None
            save_state(run_dir, state)
            record_decision(run_dir, decision)
            candidate_path = run_dir / "candidate_blueprint.json"
            if state.phase == "planning" and candidate_path.exists():
                try:
                    candidate, changes = canonicalize_blueprint_payload(self._read_json(candidate_path))
                    blueprint = ReproductionBlueprint.from_dict(candidate)
                    self._validate_blueprint_safety(blueprint)
                    self._write_blueprint_artifacts(run_dir, blueprint)
                    trace_path = run_dir / "react_trace.json"
                    trace = json.loads(trace_path.read_text()) if trace_path.exists() else []
                    if not isinstance(trace, list):
                        trace = []
                    trace.append({
                        "step": "resume_retry",
                        "event": "reused_candidate_blueprint",
                        "changes": changes,
                    })
                    write_json(trace_path, trace)
                    state.artifacts["blueprint"] = "blueprint.json"
                    self._complete_phase(run_dir, state, "planning")
                    return self._continue(run_dir, state)
                except Exception as exc:
                    write_json(run_dir / "candidate_reuse_failure.json", {
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "timestamp": time.time(),
                    })
            return self._continue(run_dir, state)
        if state.status != "waiting_human":
            raise ValueError(f"run is not waiting for human input: status={state.status}")
        record_decision(run_dir, decision)
        blueprint_data = self._read_json(run_dir / "blueprint.json")
        if decision.decision == "reject":
            state.status = "rejected"
            state.phase = "approval"
            state.pending_gate = None
            save_state(run_dir, state)
            return {"run_id": state.run_id, "run_dir": str(run_dir), "status": "rejected"}
        if decision.decision == "revise":
            blueprint_data = apply_controlled_patch(blueprint_data, decision.patch or {})
            blueprint = ReproductionBlueprint.from_dict(blueprint_data)
            self._validate_blueprint_safety(blueprint)
            self._write_blueprint_artifacts(run_dir, blueprint)
            self._reset_after_human_revision(state, gate_phase)
            state.phase = "approval"
            state.status = "running"
            state.pending_gate = None
            save_state(run_dir, state)
            reasons = gate_reasons(
                blueprint.to_dict(),
                state.evaluation_failed_rounds,
                calibration_failed_rounds=state.calibration_failed_rounds,
            )
            if reasons:
                return self._request_human(run_dir, state, reasons, "Review the revised reproduction blueprint")
        elif decision.decision == "feedback":
            input_data = self._read_json(run_dir / "input.json")
            try:
                blueprint, trace = plan_reproduction(
                    str(input_data["dba_description"]),
                    metadata=input_data.get("metadata") or {},
                    config=self.config,
                    feedback=decision.feedback,
                    previous_blueprint=blueprint_data,
                )
            except ReproductionPlanningError as exc:
                self._write_planner_failure(run_dir, state, exc, stage="feedback_replanning")
                state.status = "failed"
                state.last_error = str(exc)
                save_state(run_dir, state)
                raise
            self._validate_blueprint_safety(blueprint)
            self._write_blueprint_artifacts(run_dir, blueprint)
            write_json(run_dir / "react_trace_feedback.json", trace)
            self._reset_after_human_revision(state, gate_phase)
            state.phase = "approval"
            state.status = "running"
            state.pending_gate = None
            save_state(run_dir, state)
            reasons = gate_reasons(
                blueprint.to_dict(),
                state.evaluation_failed_rounds,
                calibration_failed_rounds=state.calibration_failed_rounds,
            )
            if reasons:
                return self._request_human(run_dir, state, reasons, "Review the blueprint regenerated from human feedback")
        else:
            if gate_phase == "calibration":
                self._approve_calibration_override(run_dir, state)
                return self._continue(run_dir, state, human_approved=True)
            state.phase = "approval"
            state.status = "running"
            state.pending_gate = None
            save_state(run_dir, state)
        self._complete_phase(run_dir, state, "approval")
        return self._continue(run_dir, state, human_approved=True)

    def _continue(self, run_dir: Path, state: RunState, *, human_approved: bool = False) -> dict[str, Any]:
        try:
            input_data = self._read_json(run_dir / "input.json")
            if "planning" not in state.completed_phases:
                try:
                    blueprint, trace = plan_reproduction(
                        str(input_data["dba_description"]),
                        metadata=input_data.get("metadata") or {},
                        config=self.config,
                    )
                except ReproductionPlanningError as exc:
                    self._write_planner_failure(run_dir, state, exc, stage="initial_planning")
                    raise
                write_json(run_dir / "candidate_blueprint.json", blueprint.to_dict())
                write_json(run_dir / "react_trace.json", trace)
                self._validate_blueprint_safety(blueprint)
                self._write_blueprint_artifacts(run_dir, blueprint)
                write_json(run_dir / "react_trace.json", trace)
                state.artifacts.update({
                    "blueprint": "blueprint.json",
                    "incident_spec": "incident_spec.json",
                    "feasibility": "feasibility.json",
                    "environment_spec": "environment_spec.json",
                    "data_spec": "data_spec.json",
                    "workload_spec": "workload_spec.json",
                    "evaluation_spec": "evaluation_spec.json",
                    "experiment_request": "experiment_request.json",
                    "task_specs": "generated_task_specs.json",
                    "react_trace": "react_trace.json",
                })
                self._complete_phase(run_dir, state, "planning")
            else:
                blueprint = ReproductionBlueprint.from_dict(self._read_json(run_dir / "blueprint.json"))

            if not human_approved and "approval" not in state.completed_phases:
                reasons = gate_reasons(
                    blueprint.to_dict(),
                    state.evaluation_failed_rounds,
                    calibration_failed_rounds=state.calibration_failed_rounds,
                )
                if reasons:
                    state.phase = "approval"
                    save_state(run_dir, state)
                    return self._request_human(run_dir, state, reasons, "Human review required before environment mutation")
                self._complete_phase(run_dir, state, "approval")

            if blueprint.feasibility.level == "blocked":
                raise RuntimeError(
                    "Reproduction is blocked by missing capabilities: "
                    + "; ".join(blueprint.feasibility.missing_capabilities)
                )
            dbms = normalize_dbms(blueprint.incident_spec.dbms)
            if dbms not in supported_execution_dbms():
                raise RuntimeError(f"No execution adapter for DBMS: {blueprint.incident_spec.dbms}")

            if "preparation" not in state.completed_phases:
                state.phase = "preparation"
                save_state(run_dir, state)
                prep = self._prepare_database(blueprint)
                write_json(run_dir / "preparation_result.json", prep)
                state.artifacts["preparation"] = "preparation_result.json"
                self._complete_phase(run_dir, state, "preparation")

            if "calibration" not in state.completed_phases:
                state.phase = "calibration"
                save_state(run_dir, state)
                blueprint, calibration = self._calibrate(run_dir, state, input_data, blueprint)
                write_json(run_dir / "calibration_result.json", calibration)
                self._write_blueprint_artifacts(run_dir, blueprint)
                state.artifacts["calibration"] = "calibration_result.json"
                if not calibration.get("matched"):
                    state.calibration_failed_rounds = max(state.calibration_failed_rounds, 1)
                    save_state(run_dir, state)
                    reasons = gate_reasons(
                        blueprint.to_dict(),
                        state.evaluation_failed_rounds,
                        calibration_failed_rounds=state.calibration_failed_rounds,
                    )
                    if calibration.get("status") == "weak_match":
                        reasons = list(dict.fromkeys([*reasons, "calibration_weak_match"]))
                        summary = "Calibration found a weak match; DBA judgment is required"
                    elif calibration.get("status") == "rejected_by_llm":
                        reasons = list(dict.fromkeys([*reasons, "calibration_rejected_by_llm"]))
                        summary = "Calibration was rejected by the LLM; DBA judgment is required"
                    else:
                        summary = "Calibration requires DBA judgment"
                    return self._request_human(
                        run_dir,
                        state,
                        reasons,
                        summary,
                        details={
                            "calibration": {
                                "status": calibration.get("status"),
                                "decision": calibration.get("decision"),
                                "concerns": calibration.get("concerns") or [],
                                "recommended_changes": calibration.get("recommended_changes") or [],
                            }
                        },
                    )
                self._complete_phase(run_dir, state, "calibration")

            if "execution" not in state.completed_phases:
                state.phase = "execution"
                save_state(run_dir, state)
                execution = self._execute(blueprint, run_dir)
                write_json(run_dir / "execution_bundle.json", execution)
                state.artifacts["execution"] = "execution_bundle.json"
                self._complete_phase(run_dir, state, "execution")
            else:
                execution = self._read_json(run_dir / "execution_bundle.json")

            if "evaluation" not in state.completed_phases:
                state.phase = "evaluation"
                save_state(run_dir, state)
                evaluation = self._evaluate(blueprint, execution)
                write_json(run_dir / "evaluation_result.json", evaluation.to_dict())
                state.artifacts["evaluation"] = "evaluation_result.json"
                if not evaluation.success:
                    state.evaluation_failed_rounds += 1
                    state.failed_rounds = state.evaluation_failed_rounds
                    save_state(run_dir, state)
                    if state.evaluation_failed_rounds >= 2:
                        return self._request_human(
                            run_dir,
                            state,
                            gate_reasons(blueprint.to_dict(), state.evaluation_failed_rounds),
                            "Reproduction failed twice; review evidence and adjust the design",
                        )
                    try:
                        revised, trace = plan_reproduction(
                            str(input_data["dba_description"]),
                            metadata=input_data.get("metadata") or {},
                            config=self.config,
                            previous_blueprint=blueprint.to_dict(),
                            observations={"execution": execution, "evaluation": evaluation.to_dict()},
                        )
                    except ReproductionPlanningError as exc:
                        self._write_planner_failure(run_dir, state, exc, stage="execution_reflection")
                        raise
                    self._validate_blueprint_safety(revised)
                    write_json(run_dir / f"blueprint_reflection_round_{state.evaluation_failed_rounds + 1}.json", revised.to_dict())
                    write_json(run_dir / f"react_trace_reflection_round_{state.evaluation_failed_rounds + 1}.json", trace)
                    self._write_blueprint_artifacts(run_dir, revised)
                    revised_gate_reasons = gate_reasons(revised.to_dict(), state.evaluation_failed_rounds)
                    if revised_gate_reasons:
                        return self._request_human(
                            run_dir,
                            state,
                            revised_gate_reasons,
                            "Review the reproduction plan revised after a failed execution",
                        )
                    for phase in ("preparation", "calibration", "execution", "evaluation"):
                        if phase in state.completed_phases:
                            state.completed_phases.remove(phase)
                    state.phase = "preparation"
                    save_state(run_dir, state)
                    return self._continue(run_dir, state, human_approved=True)
                self._complete_phase(run_dir, state, "evaluation")
            else:
                evaluation = None

            state.status = "completed"
            state.phase = "completed"
            report = {
                "run_id": state.run_id,
                "reproduction_level": blueprint.feasibility.level,
                "original_data_available": False,
                "evaluation": evaluation.to_dict() if evaluation else self._read_json(run_dir / "evaluation_result.json"),
                "unmatched_conditions": blueprint.feasibility.unmatched_conditions,
                "decision_history": "decision_history.jsonl",
            }
            write_json(run_dir / "reproduction_report.json", report)
            state.artifacts["report"] = "reproduction_report.json"
            self._complete_phase(run_dir, state, "completed")
            return {
                "run_id": state.run_id,
                "run_dir": str(run_dir),
                "status": "completed",
                "evaluation": evaluation.to_dict() if evaluation else self._read_json(run_dir / "evaluation_result.json"),
            }
        except HumanGateRequired:
            raise
        except Exception as exc:
            state.status = "failed"
            state.last_error = str(exc)
            save_state(run_dir, state)
            write_json(run_dir / "failure.json", {
                "phase": state.phase,
                "error": str(exc),
                "exception_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
                "timestamp": time.time(),
            })
            raise

    def _approve_calibration_override(self, run_dir: Path, state: RunState) -> None:
        calibration_path = run_dir / "calibration_result.json"
        if not calibration_path.exists():
            raise ValueError("calibration approval requires calibration_result.json")
        calibration = self._read_json(calibration_path)
        observed_matched = bool(calibration.get("matched"))
        override = {
            "status": "approved_by_human",
            "phase": "calibration",
            "observed_matched": observed_matched,
            "effective_matched": True,
            "calibration_failed_rounds": state.calibration_failed_rounds,
            "timestamp": time.time(),
        }
        write_json(run_dir / "calibration_override.json", override)
        calibration.update({
            "status": "overridden",
            "observed_matched": observed_matched,
            "matched": True,
            "overridden": True,
        })
        write_json(calibration_path, calibration)
        state.artifacts["calibration_override"] = "calibration_override.json"
        state.pending_gate = None
        self._complete_phase(run_dir, state, "calibration")

    @staticmethod
    def _reset_after_human_revision(state: RunState, gate_phase: str) -> None:
        if gate_phase not in {"calibration", "execution", "evaluation"}:
            return
        for phase in ("preparation", "calibration", "execution", "evaluation", "completed"):
            if phase in state.completed_phases:
                state.completed_phases.remove(phase)
        state.calibration_failed_rounds = 0
        if gate_phase in {"execution", "evaluation"}:
            state.evaluation_failed_rounds = 0
            state.failed_rounds = 0

    def _request_human(
        self,
        run_dir: Path,
        state: RunState,
        reasons: list[str],
        summary: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = write_gate(run_dir, state, phase=state.phase, reasons=reasons, summary=summary, details=details)
        if state.interaction == "checkpoint":
            raise HumanGateRequired(run_dir, request)
        decision = self._interactive_decision(request)
        return self.resume(run_dir, decision)

    def _interactive_decision(self, request: dict[str, Any]) -> HumanDecision:
        print(json.dumps(request, ensure_ascii=False, indent=2))
        choice = input("Decision [approve/reject/revise/feedback]: ").strip().lower()
        if choice == "revise":
            patch = json.loads(input("JSON Merge Patch: ").strip())
            return HumanDecision("revise", patch=patch)
        if choice == "feedback":
            return HumanDecision("feedback", feedback=input("DBA feedback: ").strip())
        return HumanDecision(choice)  # type: ignore[arg-type]

    def _prepare_mysql(self, blueprint: ReproductionBlueprint) -> dict[str, Any]:
        import pymysql

        spec = blueprint.data_spec
        database = self._safe_database_name(spec.database)
        executed: list[str] = []
        connection = pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
            autocommit=True,
            connect_timeout=5,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
            connection.select_db(database)
            with connection.cursor() as cursor:
                for sql in spec.schema_sql:
                    self._validate_setup_sql(sql)
                    cursor.execute(sql)
                    executed.append(sql)
                for sql in spec.generation_sql:
                    statement = sql.format(row_count=spec.scale_strategy.initial_rows)
                    self._validate_setup_sql(statement)
                    cursor.execute(statement)
                    executed.append(statement)
                for table in spec.analyze_tables:
                    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
                        raise ValueError(f"unsafe table name in analyze_tables: {table}")
                    cursor.execute(f"ANALYZE TABLE `{table}`")
        finally:
            connection.close()
        return {"database": database, "statement_count": len(executed), "statements": executed}

    def _prepare_database(self, blueprint: ReproductionBlueprint) -> dict[str, Any]:
        dbms = normalize_dbms(blueprint.incident_spec.dbms)
        if dbms == "postgresql":
            spec = blueprint.data_spec
            database = self._safe_database_name(spec.database)
            return PostgresAdapter().prepare(
                database,
                list(spec.schema_sql),
                list(spec.generation_sql),
                list(spec.analyze_tables),
                row_count=spec.scale_strategy.initial_rows,
            )
        if dbms == "sqlserver":
            spec = blueprint.data_spec
            database = self._safe_database_name(spec.database)
            return SqlServerAdapter().prepare(
                database,
                list(spec.schema_sql),
                list(spec.generation_sql),
                list(spec.analyze_tables),
                row_count=spec.scale_strategy.initial_rows,
            )
        return self._prepare_mysql(blueprint)

    def _calibrate(
        self,
        run_dir: Path,
        state: RunState,
        input_data: dict[str, Any],
        blueprint: ReproductionBlueprint,
    ) -> tuple[ReproductionBlueprint, dict[str, Any]]:
        if not blueprint.data_spec.calibration_queries:
            state.calibration_failed_rounds = 0
            save_state(run_dir, state)
            return blueprint, {"status": "not_applicable", "matched": True, "rounds": []}
        rounds: list[dict[str, Any]] = []
        preparation_path = run_dir / "preparation_result.json"
        preparation = self._read_json(preparation_path) if preparation_path.exists() else {}
        round_no = 1
        try:
            decision, _revised, trace = calibrate_reproduction(
                str(input_data["dba_description"]),
                blueprint,
                preparation,
                round_no=round_no,
                previous_calibration=None,
                config=self.config,
            )
        except ReproductionPlanningError as exc:
            write_json(run_dir / f"react_trace_calibration_round_{round_no}.json", exc.trace)
            self._write_planner_failure(run_dir, state, exc, stage=f"calibration_round_{round_no}")
            raise
        status_by_decision = {
            "accept": "accepted",
            "weak_match": "weak_match",
            "reject": "rejected_by_llm",
        }
        matched = decision["decision"] == "accept"
        round_result = {**decision, "round": round_no, "matched": matched}
        rounds.append(round_result)
        write_json(run_dir / f"calibration_round_{round_no}.json", round_result)
        write_json(run_dir / f"react_trace_calibration_round_{round_no}.json", trace)
        if matched:
            state.calibration_failed_rounds = 0
        else:
            state.calibration_failed_rounds = 1
        save_state(run_dir, state)
        return blueprint, {
            "status": status_by_decision[decision["decision"]],
            "matched": matched,
            "decision": decision["decision"],
            "weak_match": decision["decision"] == "weak_match",
            "concerns": decision.get("concerns") or [],
            "recommended_changes": decision.get("recommended_changes") or [],
            "rounds": rounds,
        }

    def _execute(self, blueprint: ReproductionBlueprint, run_dir: Path) -> dict[str, Any]:
        if normalize_dbms(blueprint.incident_spec.dbms) in {"postgresql", "sqlserver"}:
            return self._execute_external_dbms(blueprint, run_dir)
        dag = agent_tools.build_task_dag(blueprint.task_specs, blueprint.dependencies)
        request = blueprint.experiment_request
        safety = self._check_external_safety(blueprint, dag)
        write_json(run_dir / "task_dag.json", dag)
        write_json(run_dir / "safety.json", safety)
        if not safety.get("approved"):
            raise RuntimeError("TaskSpec safety check failed: " + "; ".join(safety.get("reasons", [])))
        database = blueprint.data_spec.database
        slow_log_probe = SlowLogProbe(self.config) if is_slow_log_reproduction(blueprint.to_dict()) else None
        slow_log_marker = slow_log_probe.marker() if slow_log_probe else None
        if slow_log_marker is not None:
            write_json(run_dir / "slow_log_marker.json", slow_log_marker)
        baseline = agent_tools.collect_baseline_metrics(self.config, database)
        with self._background_workload(blueprint):
            trace = agent_tools.execute_dag(
                dag,
                self.config,
                max_duration_sec=int(request.get("max_duration_sec", self.config.max_duration_sec)),
                round_dir=str(run_dir),
            )
        after = agent_tools.collect_baseline_metrics(self.config, database)
        slow_log_evidence = slow_log_probe.collect(slow_log_marker or {}) if slow_log_probe else None
        if slow_log_evidence is not None:
            calibration_path = run_dir / "calibration_result.json"
            slow_log_evidence["calibration"] = self._read_json(calibration_path) if calibration_path.exists() else {}
            write_json(run_dir / "slow_log_evidence.json", slow_log_evidence)
        return {
            "baseline": baseline,
            "execution_trace": trace,
            "after": after,
            "task_dag": dag,
            "safety": safety,
            "slow_log_evidence": slow_log_evidence,
        }

    def _execute_external_dbms(self, blueprint: ReproductionBlueprint, run_dir: Path) -> dict[str, Any]:
        dbms = normalize_dbms(blueprint.incident_spec.dbms)
        adapter = SqlServerAdapter() if dbms == "sqlserver" else PostgresAdapter()
        dag = agent_tools.build_task_dag(blueprint.task_specs, blueprint.dependencies)
        request = blueprint.experiment_request
        safety = self._check_external_safety(blueprint, dag)
        write_json(run_dir / "task_dag.json", dag)
        write_json(run_dir / "safety.json", safety)
        if not safety.get("approved"):
            raise RuntimeError("TaskSpec safety check failed: " + "; ".join(safety.get("reasons", [])))
        before = {
            "schema": adapter.schema(blueprint.data_spec.database),
            "table_stats": adapter.table_stats(blueprint.data_spec.database),
            "db_metrics": adapter.db_metrics(blueprint.data_spec.database),
        }
        trace = {"tasks": {}, "cleanup_status": "not_started", "cleanup_errors": []}
        with self._background_workload(blueprint):
            for task in blueprint.task_specs:
                task_id = str(task.get("task_id") or f"task_{len(trace['tasks']) + 1}")
                result = {"task_id": task_id, "status": "success", "actions": [], "errors": []}
                for action in task.get("actions") or []:
                    try:
                        kind = action.get("kind")
                        if kind == "raw_sql_workload":
                            action_result = adapter.run_sql_workload(action, task_id=task_id, round_dir=run_dir)
                        elif kind == "raw_transaction_script":
                            action_result = adapter.run_transaction_script(action)
                        elif kind == "raw_command":
                            action_result = self._run_external_raw_command(action, task_id=task_id, round_dir=run_dir)
                        else:
                            raise RuntimeError(f"{dbms} executor does not support action kind: {kind}")
                        result["actions"].append(action_result)
                    except Exception as exc:
                        result["status"] = "failed"
                        result["errors"].append(str(exc))
                        break
                trace["tasks"][task_id] = result
                if result["status"] != "success":
                    break
            trace["cleanup_status"] = "success"
        failed = [task for task in trace["tasks"].values() if task.get("status") != "success"]
        if failed:
            raise RuntimeError(f"{dbms} TaskSpec execution failed: " + json.dumps(failed[:3], ensure_ascii=False))
        after = {
            "schema": adapter.schema(blueprint.data_spec.database),
            "table_stats": adapter.table_stats(blueprint.data_spec.database),
            "db_metrics": adapter.db_metrics(blueprint.data_spec.database),
        }
        return {"baseline": before, "execution_trace": trace, "after": after, "task_dag": dag, "safety": safety}

    def _check_external_safety(self, blueprint: ReproductionBlueprint, dag: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        max_duration = float((blueprint.experiment_request or {}).get("max_duration_sec", self.config.max_duration_sec))
        for task in blueprint.task_specs:
            for action in task.get("actions") or []:
                duration = float(action.get("duration_sec") or 0)
                if duration <= 0 or duration > max_duration:
                    reasons.append(f"action duration {duration} exceeds allowed window {max_duration}")
                text = json.dumps(action, ensure_ascii=False).lower()
                if re.search(r"\bdrop\s+database\b|\btruncate\b|\bshutdown\b|rm\s+-rf|mkfs|reboot", text, re.I):
                    reasons.append("dangerous operation in external DBMS TaskSpec")
                if action.get("kind") not in {"raw_sql_workload", "raw_transaction_script", "raw_command"}:
                    reasons.append(f"unsupported external DBMS action kind: {action.get('kind')}")
        return {
            "approved": not reasons,
            "reasons": reasons,
            "scope": "InputAnalysisAgent external DBMS dedicated database safety",
            "dag_task_count": len((dag or {}).get("tasks") or []),
        }

    def _run_external_raw_command(self, action: dict[str, Any], *, task_id: str, round_dir: Path) -> dict[str, Any]:
        command = action.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("raw_command.command must be a non-empty string array")
        duration_sec = float(action.get("duration_sec") or 1)
        started = time.time()
        proc = subprocess.Popen(
            command,
            cwd=str(action.get("cwd") or round_dir),
            env={**os.environ, **{k: str(v) for k, v in (action.get("env") or {}).items()}},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=duration_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=5)
        cleanup_result = None
        cleanup = action.get("cleanup_command")
        if isinstance(cleanup, list) and cleanup and all(isinstance(item, str) and item for item in cleanup):
            cleanup_proc = subprocess.run(
                cleanup,
                cwd=str(action.get("cwd") or round_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            cleanup_result = {
                "command": cleanup,
                "exit_code": cleanup_proc.returncode,
                "stdout_tail": cleanup_proc.stdout[-2000:],
                "stderr_tail": cleanup_proc.stderr[-2000:],
            }
        artifact = round_dir / f"{task_id}_raw_command.json"
        result = {
            "kind": "raw_command",
            "command": command,
            "duration_sec": duration_sec,
            "timed_out": timed_out,
            "exit_code": proc.returncode,
            "elapsed_sec": time.time() - started,
            "stdout_tail": (stdout or "")[-2000:],
            "stderr_tail": (stderr or "")[-2000:],
            "cleanup": cleanup_result,
        }
        write_json(artifact, result)
        if proc.returncode not in (0, None) and not timed_out:
            raise RuntimeError(f"raw_command exited with code {proc.returncode}: {(stderr or stdout or '')[-500:]}")
        return {**result, "artifact": str(artifact)}

    def _evaluate(self, blueprint: ReproductionBlueprint, execution: dict[str, Any]) -> ReproductionEvaluation:
        objective = None
        if is_slow_log_reproduction(blueprint.to_dict()):
            objective = evaluate_slow_log_evidence(
                execution.get("slow_log_evidence") or {},
                blueprint.to_dict(),
            )
        try:
            evaluation = evaluate_reproduction(blueprint, execution, config=self.config)
        except ReproductionPlanningError:
            if objective is None:
                raise
            evaluation = ReproductionEvaluation(
                symptom_hit=False,
                mechanism_hit=False,
                plan_similarity=1.0 if objective.get("calibration_matched") else 0.0,
                success=False,
                reason="LLM evaluation unavailable; objective slow-log evidence was used.",
            )
        if objective is not None:
            evaluation.symptom_hit = bool(objective["symptom_hit"])
            evaluation.mechanism_hit = bool(objective["mechanism_hit"])
            evaluation.plan_similarity = 1.0 if objective.get("calibration_matched") else 0.0
            minimum = float(blueprint.evaluation_spec.get("minimum_plan_similarity", 0.0) or 0.0)
            evaluation.success = bool(objective["success"]) and evaluation.plan_similarity >= minimum
            evaluation.reason = str(objective["reason"])
            evaluation.evidence = {**evaluation.evidence, "slow_log": objective}
            if not objective.get("available"):
                evaluation.unmatched_conditions.append("Slow log was not readable through FILE or mysql.slow_log TABLE")
        return evaluation

    @contextmanager
    def _background_workload(self, blueprint: ReproductionBlueprint) -> Iterator[None]:
        spec = blueprint.workload_spec
        if not spec.get("enabled") or spec.get("method") in {None, "none"}:
            yield
            return
        if spec.get("method") != "sql":
            raise RuntimeError(f"Unsupported InputAnalysis workload method: {spec.get('method')}")
        queries = spec.get("queries") or []
        if not queries or not all(isinstance(query, str) for query in queries):
            raise ValueError("SQL background workload requires query strings")
        stop = threading.Event()
        errors: list[str] = []
        external_dbms = normalize_dbms(blueprint.incident_spec.dbms)
        if external_dbms in {"postgresql", "sqlserver"}:
            adapter = SqlServerAdapter() if external_dbms == "sqlserver" else PostgresAdapter()

            def pg_worker() -> None:
                try:
                    while not stop.is_set():
                        for query in queries:
                            if stop.is_set():
                                break
                            adapter.execute(blueprint.data_spec.database, query, timeout=30)
                except Exception as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=pg_worker, daemon=True) for _ in range(max(1, int(spec.get("concurrency", 1))))]
            for thread in threads:
                thread.start()
            try:
                yield
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=5)
            if errors:
                raise RuntimeError(f"background workload failed: {errors[:3]}")
            return

        def worker() -> None:
            import pymysql
            try:
                connection = pymysql.connect(
                    host=self.config.mysql_host,
                    port=self.config.mysql_port,
                    user=self.config.mysql_user,
                    password=self.config.mysql_password,
                    database=blueprint.data_spec.database,
                    autocommit=True,
                    connect_timeout=5,
                )
                with connection.cursor() as cursor:
                    while not stop.is_set():
                        for query in queries:
                            if stop.is_set():
                                break
                            cursor.execute(query)
                            try:
                                cursor.fetchmany(10)
                            except Exception:
                                pass
                connection.close()
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, int(spec.get("concurrency", 1))))]
        for thread in threads:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        if errors:
            raise RuntimeError(f"background workload failed: {errors[:3]}")

    def _validate_blueprint_safety(self, blueprint: ReproductionBlueprint) -> None:
        self._safe_database_name(blueprint.data_spec.database)
        for statement in blueprint.data_spec.schema_sql + blueprint.data_spec.generation_sql:
            self._validate_setup_sql(statement)
        dag = agent_tools.build_task_dag(blueprint.task_specs, blueprint.dependencies)
        if normalize_dbms(blueprint.incident_spec.dbms) in {"postgresql", "sqlserver"}:
            result = self._check_external_safety(blueprint, dag)
            if not result.get("approved"):
                raise ValueError("unsafe blueprint TaskSpecs: " + "; ".join(result.get("reasons", [])))
            return
        request = blueprint.experiment_request
        result = agent_tools.check_safety(
            dag,
            self.config,
            max_duration_sec=float(request.get("max_duration_sec", self.config.max_duration_sec)),
            expected_workload=request.get("workload") or {},
        )
        if not result.get("approved"):
            raise ValueError("unsafe blueprint TaskSpecs: " + "; ".join(result.get("reasons", [])))

    def _validate_setup_sql(self, sql: str) -> None:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("setup SQL statements must be non-empty strings")
        if any(pattern.search(sql) for pattern in _UNSAFE_SETUP_SQL):
            raise ValueError(f"unsafe setup SQL: {sql[:160]}")

    def _safe_database_name(self, database: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", database):
            raise ValueError(f"unsafe database name: {database}")
        if re.search(r"(^|_)(prod|production|live)($|_)", database, re.I):
            raise ValueError(f"database name looks like production: {database}")
        return database

    def _complete_phase(self, run_dir: Path, state: RunState, phase: str) -> None:
        if phase not in state.completed_phases:
            state.completed_phases.append(phase)
        state.phase = phase
        state.status = "completed" if phase == "completed" else "running"
        save_state(run_dir, state)

    @staticmethod
    def _write_planner_failure(
        run_dir: Path,
        state: RunState,
        error: ReproductionPlanningError,
        *,
        stage: str,
    ) -> None:
        write_json(run_dir / "react_trace.json", error.trace)
        write_json(run_dir / "planner_failure.json", {
            "status": "failed",
            "stage": stage,
            "reason": error.reason,
            "trace": error.trace,
            "retry_command": (
                f"python3 -m InputAnalysisAgent.cli resume --run-dir {run_dir} --decision retry"
            ),
            "timestamp": time.time(),
        })
        if error.candidate is not None:
            write_json(run_dir / "candidate_blueprint.json", error.candidate)
            state.artifacts["candidate_blueprint"] = "candidate_blueprint.json"
        state.artifacts["react_trace"] = "react_trace.json"
        state.artifacts["planner_failure"] = "planner_failure.json"
        save_state(run_dir, state)

    @staticmethod
    def _write_blueprint_artifacts(run_dir: Path, blueprint: ReproductionBlueprint) -> None:
        write_json(run_dir / "incident_spec.json", blueprint.incident_spec.to_dict())
        write_json(run_dir / "feasibility.json", blueprint.feasibility.to_dict())
        write_json(run_dir / "environment_spec.json", blueprint.environment_spec)
        write_json(run_dir / "data_spec.json", blueprint.data_spec.to_dict())
        write_json(run_dir / "workload_spec.json", blueprint.workload_spec)
        write_json(run_dir / "evaluation_spec.json", blueprint.evaluation_spec)
        write_json(run_dir / "experiment_request.json", blueprint.experiment_request)
        write_json(run_dir / "generated_task_specs.json", {
            "task_specs": blueprint.task_specs,
            "dependencies": blueprint.dependencies,
        })
        write_json(run_dir / "blueprint.json", blueprint.to_dict())

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object in {path}")
        return value
