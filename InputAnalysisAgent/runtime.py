"""Resumable runtime for reproducing anomalies described by DBA posts."""

from __future__ import annotations

import json
import re
import threading
import time
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
from InputAnalysisAgent.react import ReproductionPlanningError, evaluate_reproduction, plan_reproduction
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
        decision.validate()
        if decision.decision == "retry":
            if state.status != "failed":
                raise ValueError(f"retry requires a failed run: status={state.status}")
            state.status = "running"
            state.last_error = ""
            state.pending_gate = None
            save_state(run_dir, state)
            record_decision(run_dir, decision)
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
            state.phase = "approval"
            state.status = "running"
            state.pending_gate = None
            save_state(run_dir, state)
            reasons = gate_reasons(blueprint.to_dict(), state.failed_rounds)
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
            state.phase = "approval"
            state.status = "running"
            state.pending_gate = None
            save_state(run_dir, state)
            reasons = gate_reasons(blueprint.to_dict(), state.failed_rounds)
            if reasons:
                return self._request_human(run_dir, state, reasons, "Review the blueprint regenerated from human feedback")
        else:
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
                reasons = gate_reasons(blueprint.to_dict(), state.failed_rounds)
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
            if blueprint.incident_spec.dbms not in {"mysql", "mariadb", "percona"}:
                raise RuntimeError(f"No execution adapter for DBMS: {blueprint.incident_spec.dbms}")

            if "preparation" not in state.completed_phases:
                state.phase = "preparation"
                save_state(run_dir, state)
                prep = self._prepare_mysql(blueprint)
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
                    state.failed_rounds += 1
                    save_state(run_dir, state)
                    reasons = gate_reasons(blueprint.to_dict(), max(2, state.failed_rounds))
                    return self._request_human(run_dir, state, reasons, "Calibration failed twice; DBA judgment is required")
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
                    state.failed_rounds += 1
                    save_state(run_dir, state)
                    if state.failed_rounds >= 2:
                        return self._request_human(
                            run_dir,
                            state,
                            gate_reasons(blueprint.to_dict(), state.failed_rounds),
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
                    write_json(run_dir / f"blueprint_reflection_round_{state.failed_rounds + 1}.json", revised.to_dict())
                    write_json(run_dir / f"react_trace_reflection_round_{state.failed_rounds + 1}.json", trace)
                    self._write_blueprint_artifacts(run_dir, revised)
                    revised_gate_reasons = gate_reasons(revised.to_dict(), state.failed_rounds)
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
            write_json(run_dir / "failure.json", {"phase": state.phase, "error": str(exc), "timestamp": time.time()})
            raise

    def _request_human(self, run_dir: Path, state: RunState, reasons: list[str], summary: str) -> dict[str, Any]:
        request = write_gate(run_dir, state, phase=state.phase, reasons=reasons, summary=summary)
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

    def _calibrate(
        self,
        run_dir: Path,
        state: RunState,
        input_data: dict[str, Any],
        blueprint: ReproductionBlueprint,
    ) -> tuple[ReproductionBlueprint, dict[str, Any]]:
        rounds: list[dict[str, Any]] = []
        max_rounds = min(blueprint.data_spec.scale_strategy.max_rounds, 2)
        for round_no in range(1, max_rounds + 1):
            observations = self._calibration_observations(blueprint)
            matched = all(item.get("matched", False) for item in observations) if observations else True
            round_result = {"round": round_no, "matched": matched, "observations": observations}
            rounds.append(round_result)
            write_json(run_dir / f"calibration_round_{round_no}.json", round_result)
            if matched:
                return blueprint, {"matched": True, "rounds": rounds}
            if round_no < max_rounds:
                try:
                    revised, trace = plan_reproduction(
                        str(input_data["dba_description"]),
                        metadata=input_data.get("metadata") or {},
                        config=self.config,
                        previous_blueprint=blueprint.to_dict(),
                        observations=round_result,
                    )
                except ReproductionPlanningError as exc:
                    self._write_planner_failure(run_dir, state, exc, stage=f"calibration_round_{round_no}")
                    raise
                self._validate_blueprint_safety(revised)
                blueprint = revised
                write_json(run_dir / f"blueprint_calibration_round_{round_no + 1}.json", blueprint.to_dict())
                write_json(run_dir / f"react_trace_calibration_round_{round_no + 1}.json", trace)
                self._prepare_mysql(blueprint)
        state.failed_rounds = max(state.failed_rounds, 2)
        return blueprint, {"matched": False, "rounds": rounds}

    def _calibration_observations(self, blueprint: ReproductionBlueprint) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in blueprint.data_spec.calibration_queries:
            sql = str(item["sql"])
            if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
                raise ValueError("calibration queries must be read-only SELECT/WITH statements")
            explained = agent_tools.explain_sql(self.config, blueprint.data_spec.database, sql)
            text = json.dumps(explained, ensure_ascii=False).lower()
            expected = [str(value).lower() for value in item.get("expected_plan_features", [])]
            started = time.monotonic()
            probe = self._probe_read_query(blueprint.data_spec.database, sql, float(item.get("max_probe_sec", 10)))
            elapsed = time.monotonic() - started
            results.append({
                "sql": sql,
                "explain": explained,
                "expected_plan_features": expected,
                "matched_features": [feature for feature in expected if feature in text],
                "matched": not expected or all(feature in text for feature in expected),
                "probe": probe,
                "elapsed_sec": elapsed,
            })
        return results

    def _probe_read_query(self, database: str, sql: str, timeout_sec: float) -> dict[str, Any]:
        import pymysql

        result: dict[str, Any] = {"completed": False, "row_count": 0, "error": ""}

        def run() -> None:
            connection = None
            try:
                connection = pymysql.connect(
                    host=self.config.mysql_host,
                    port=self.config.mysql_port,
                    user=self.config.mysql_user,
                    password=self.config.mysql_password,
                    database=database,
                    read_timeout=max(1, int(timeout_sec)),
                    connect_timeout=5,
                )
                with connection.cursor() as cursor:
                    cursor.execute(sql)
                    rows = cursor.fetchmany(1000)
                    result.update({"completed": True, "row_count": len(rows)})
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                if connection:
                    connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=max(0.1, timeout_sec))
        if thread.is_alive():
            result["timed_out"] = True
        return result

    def _execute(self, blueprint: ReproductionBlueprint, run_dir: Path) -> dict[str, Any]:
        dag = agent_tools.build_task_dag(blueprint.task_specs, blueprint.dependencies)
        request = blueprint.experiment_request
        safety = agent_tools.check_safety(
            dag,
            self.config,
            max_duration_sec=float(request.get("max_duration_sec", self.config.max_duration_sec)),
            expected_workload=request.get("workload") or {},
        )
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
