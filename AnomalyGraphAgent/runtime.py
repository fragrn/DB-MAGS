"""
DBMAGS Runtime: orchestrates the full experiment lifecycle.

Workflow per round:
  1. inspect  — probe environment
  2. plan     — ReAct loop -> ExecutableTaskDAG
  3. safety   — pre-execution safety gate
  4. baseline — collect metrics before injection
  5. execute  — run the task DAG
  6. after    — collect metrics after injection
  7. evaluate — graph-driven node/path evaluation
  8. reflect  — on failure, generate suggestions and update memory
  9. retry    — loop up to max_retry_rounds
  10. report  — write artifacts and Markdown report
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import RuntimeConfig
from agent.dag import build_task_dag
from agent.evaluator import evaluate
from agent.memory import MemoryStore
from agent.planner import GlobalPlanner
from agent.planner import PlannerFallbackError
from agent.reflection import SelfReflection
from agent.reflection import ReflectionFallbackError
from agent.safety import SafetyChecker
from agent.safety import EXECUTOR_GRACE_SEC
from agent.safety import estimate_dag_required_sec
from agent.workload import (
    make_evaluation_pair,
    make_metrics_collector,
    make_workload_runner,
    normalize_workload_config,
)
from agent.probes.mysql_probe import MySQLProbe
from agent.probes.slow_log import SlowLogProbe
from agent.types import (
    EnvironmentSnapshot,
    EvaluationResult,
    ExecutableTaskDAG,
    ExperimentRequest,
    ReActStep,
    ReflectionResult,
    RunResult,
    SafetyResult,
    to_jsonable,
)
from agent import tools as tool_registry


class DBMAGSRuntime:
    """
    Main runtime orchestrator for anomaly propagation experiments.

    Example usage:
        config = RuntimeConfig.from_env()
        runtime = DBMAGSRuntime(config)
        request = ExperimentRequest(target_anomaly="missing_index", target_database="tpcc")
        result = runtime.run(request, output_root="experiment_runs")
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.planner = GlobalPlanner(config)
        self.reflector = SelfReflection(config)
        self.safety_checker = SafetyChecker(config)
        self.memory = MemoryStore(config.memory_file)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def inspect(self, request: ExperimentRequest) -> EnvironmentSnapshot:
        """Run the full environment inspection probe."""
        return self.planner.inspect(request)

    def run(
        self,
        request: ExperimentRequest,
        output_root: str = "experiment_runs",
    ) -> RunResult:
        """
        Execute the full experiment loop with retry support.

        Returns a RunResult containing snapshot, DAG, evaluation, reflection,
        and execution trace.
        """
        run_id = _generate_run_id()
        output_dir = Path(output_root) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_run_identity(output_dir, run_id, request)

        max_rounds = request.max_retry_rounds or self.config.max_retry_rounds
        best_result: RunResult | None = None
        latest_reflection: ReflectionResult | None = None

        for round_no in range(1, max_rounds + 1):
            round_dir = output_dir / f"round_{round_no}"
            round_dir.mkdir(parents=True, exist_ok=True)

            if request.workload.get("enabled"):
                best_result, latest_reflection, should_stop = self._run_workload_round(
                    request=request,
                    run_id=run_id,
                    output_dir=output_dir,
                    round_no=round_no,
                    round_dir=round_dir,
                    latest_reflection=latest_reflection,
                )
                if should_stop:
                    self._write_report(best_result)
                    return best_result
                continue

            # Step 1: Inspect
            snapshot = self._inspect(request, round_no, round_dir)

            # Step 2: Plan
            dag, snapshot, react_trace = self._plan(request, snapshot, round_no, round_dir, latest_reflection)

            # Step 3: Safety check
            safety = self._safety_check(dag, snapshot, round_dir, request=request)

            if not safety.approved:
                # Cannot proceed — write failure and stop
                failure_eval = EvaluationResult(
                    success=False,
                    reason=f"Safety check failed: {'; '.join(safety.reasons)}",
                    safety_violations=safety.reasons,
                )
                self._write_round_artifacts(
                    round_dir, request, snapshot, dag, None, failure_eval, None, react_trace, safety,
                )
                break

            # Step 4: Baseline metrics
            baseline = self._collect_baseline(request, round_dir)

            # Step 5: Execute
            slow_log_probe, slow_log_marker = self._start_slow_log_capture(round_dir)
            try:
                execution_trace = self._execute(dag, round_no, round_dir, request)
            finally:
                slow_log_evidence = self._finish_slow_log_capture(
                    slow_log_probe,
                    slow_log_marker,
                    request.target_database,
                    round_dir,
                )

            # Step 6: After metrics
            after = self._collect_after(request, round_dir)
            after["slow_log_evidence"] = slow_log_evidence

            # Step 7: Evaluate
            evaluation = self._evaluate(
                request=request,
                baseline=baseline,
                after=after,
                dag=dag,
                target_path=request.target_path,
                execution_trace=execution_trace,
                round_dir=round_dir,
            )

            # Write round artifacts
            self._write_round_artifacts(
                round_dir, request, snapshot, dag, execution_trace,
                evaluation, None, react_trace, safety,
            )

            # Step 8: Check success
            if evaluation.success:
                best_result = RunResult(
                    run_id=run_id,
                    request=request,
                    snapshot=snapshot,
                    dag=dag,
                    evaluation=evaluation,
                    reflection=None,
                    execution_trace=execution_trace,
                    output_dir=str(output_dir),
                    rounds=round_no,
                )
                self._write_report(best_result)
                return best_result

            # Step 9: Reflect + memory
            reflection = self._reflect(evaluation, request, round_no, round_dir)
            latest_reflection = reflection
            self._update_memory(evaluation, request, reflection, round_no)

            # Log for next round
            best_result = RunResult(
                run_id=run_id,
                request=request,
                snapshot=snapshot,
                dag=dag,
                evaluation=evaluation,
                reflection=reflection,
                execution_trace=execution_trace,
                output_dir=str(output_dir),
                rounds=round_no,
            )

        # All rounds exhausted
        if best_result is None:
            best_result = RunResult(
                run_id=run_id,
                request=request,
                snapshot=EnvironmentSnapshot(database=request.target_database),
                dag=ExecutableTaskDAG(),
                evaluation=EvaluationResult(success=False, reason="No rounds executed"),
                output_dir=str(output_dir),
                rounds=0,
            )
        self._write_report(best_result)
        return best_result

    def _run_workload_round(
        self,
        request: ExperimentRequest,
        run_id: str,
        output_dir: Path,
        round_no: int,
        round_dir: Path,
        latest_reflection: ReflectionResult | None,
    ) -> tuple[RunResult, ReflectionResult | None, bool]:
        workload_cfg = normalize_workload_config(request.workload, request.target_database)
        workload_cfg["jar_path"] = self.config.benchbase_jar_path
        workload_cfg["java_bin"] = self.config.benchbase_java_bin
        if workload_cfg.get("duration_sec") is None:
            workload_cfg["duration_sec"] = self._workload_required_sec(request, workload_cfg)
        request.workload = dict(workload_cfg)
        self._write_json(round_dir / "workload_config.json", workload_cfg)
        self._validate_workload_timing_before_start(request, workload_cfg, round_dir)
        workload_trace: dict[str, Any] = {"config": workload_cfg, "events": [], "samples": []}
        runner = self._make_workload_runner(request, round_dir)
        collector = self._make_metrics_collector(request, runner)
        execution_trace: dict[str, Any] | None = None
        evaluation = EvaluationResult(success=False, reason="Workload round did not complete")
        reflection: ReflectionResult | None = None
        dag = ExecutableTaskDAG()
        react_trace: list[ReActStep] = []
        safety = SafetyResult()
        should_stop = False

        try:
            # Phase 0: static inspect without background workload.
            static_snapshot = self._inspect_named(request, round_no, round_dir, "static_snapshot.json")

            # Phase 1-2: start background workload and warm up.
            workload_trace["events"].append(runner.start())
            self._write_json(round_dir / "workload_trace.json", workload_trace)
            self._assert_workload_running(runner, "after_start_workload", round_dir, workload_trace)
            self._sleep_phase(workload_cfg.get("warmup_sec", 0), "warmup", workload_trace)
            self._assert_workload_running(runner, "after_warmup_before_runtime_inspect", round_dir, workload_trace)

            # Phase 3: inspect while workload is running.
            runtime_snapshot = self._inspect_named(request, round_no, round_dir, "runtime_snapshot.json")

            # Phase 4: collect baseline time-series while workload is running.
            self._assert_workload_running(runner, "before_baseline_sampling", round_dir, workload_trace)
            baseline_window = collector.collect_window(
                "baseline",
                workload_cfg["baseline_sec"],
                workload_cfg["sample_interval_sec"],
            )
            workload_trace["samples"].extend(baseline_window.get("samples", []))
            self._write_json(round_dir / "baseline_metrics.json", baseline_window)
            runtime_snapshot.workload_status = {
                "phase": "baseline",
                "summary": baseline_window.get("summary", {}),
                "sample_count": baseline_window.get("sample_count", 0),
            }

            # Phase 5: ReAct planning with runtime workload context.
            dag, runtime_snapshot, react_trace = self._plan(
                request, runtime_snapshot, round_no, round_dir, latest_reflection
            )
            self._assert_workload_running(runner, "after_planning_before_safety", round_dir, workload_trace)

            # Phase 6: safety while workload keeps running.
            safety = self._safety_check(dag, runtime_snapshot, round_dir, request=request)
            if not safety.approved:
                evaluation = EvaluationResult(
                    success=False,
                    reason=f"Safety check failed: {'; '.join(safety.reasons)}",
                    safety_violations=safety.reasons,
                )
                best_result = RunResult(
                    run_id=run_id,
                    request=request,
                    snapshot=runtime_snapshot,
                    dag=dag,
                    evaluation=evaluation,
                    execution_trace=None,
                    workload_trace=workload_trace,
                    output_dir=str(output_dir),
                    rounds=round_no,
                )
                self._write_round_artifacts(
                    round_dir, request, runtime_snapshot, dag, None, evaluation, None, react_trace, safety, workload_trace
                )
                should_stop = True
                return best_result, None, True

            # Phase 7: execute anomaly DAG and collect injection samples concurrently.
            self._assert_workload_running(runner, "after_safety_before_injection", round_dir, workload_trace)
            slow_log_probe, slow_log_marker = self._start_slow_log_capture(round_dir)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self._execute, dag, round_no, round_dir, request)
                    injection_window = collector.collect_window(
                        "injection",
                        workload_cfg["injection_observe_sec"],
                        workload_cfg["sample_interval_sec"],
                    )
                    workload_trace["samples"].extend(injection_window.get("samples", []))
                    self._write_json(round_dir / "injection_metrics.json", injection_window)
                    try:
                        execution_trace = future.result(timeout=float(request.max_duration_sec) + EXECUTOR_GRACE_SEC)
                    except TimeoutError:
                        execution_trace = {
                            "tasks": {},
                            "cleanup_status": "unknown",
                            "cleanup_errors": ["anomaly executor timed out after injection sampling"],
                        }
                        self._write_json(round_dir / "execution_trace.json", execution_trace)
            finally:
                slow_log_evidence = self._finish_slow_log_capture(
                    slow_log_probe,
                    slow_log_marker,
                    request.target_database,
                    round_dir,
                )
            self._assert_execution_success(execution_trace, round_dir, workload_trace)

            # Phase 9: recovery samples after anomaly cleanup, before stopping workload.
            self._assert_workload_running(runner, "after_injection_before_recovery", round_dir, workload_trace)
            recovery_window = collector.collect_window(
                "recovery",
                workload_cfg["recovery_sec"],
                workload_cfg["sample_interval_sec"],
            )
            workload_trace["samples"].extend(recovery_window.get("samples", []))
            self._write_json(round_dir / "recovery_metrics.json", recovery_window)
            self._assert_workload_running(runner, "after_recovery_before_stop_workload", round_dir, workload_trace)

            # Phase 11: evaluate windows.
            baseline_eval, injection_eval = make_evaluation_pair(baseline_window, injection_window)
            injection_eval["slow_log_evidence"] = slow_log_evidence
            evaluation = self._evaluate(
                request=request,
                baseline=baseline_eval,
                after=injection_eval,
                dag=dag,
                target_path=request.target_path,
                execution_trace=execution_trace or {},
                round_dir=round_dir,
            )
            evaluation.baseline_metrics = baseline_window
            evaluation.after_metrics = injection_window
            self._write_json(round_dir / "evaluation_result.json", to_jsonable(evaluation))
            self._write_round_observation_summary(
                round_dir=round_dir,
                round_no=round_no,
                baseline_window=baseline_window,
                injection_window=injection_window,
                recovery_window=recovery_window,
                execution_trace=execution_trace or {},
                slow_log_evidence=slow_log_evidence,
                evaluation=evaluation,
            )
            self._write_round_artifacts(
                round_dir, request, runtime_snapshot, dag, execution_trace,
                evaluation, None, react_trace, safety, workload_trace,
            )
            self._write_reflection_comparison(output_dir)

            if evaluation.success:
                best_result = RunResult(
                    run_id=run_id,
                    request=request,
                    snapshot=runtime_snapshot,
                    dag=dag,
                    evaluation=evaluation,
                    reflection=None,
                    execution_trace=execution_trace,
                    workload_trace=workload_trace,
                    output_dir=str(output_dir),
                    rounds=round_no,
                )
                should_stop = True
            else:
                reflection = self._reflect(evaluation, request, round_no, round_dir)
                self._update_memory(evaluation, request, reflection, round_no)
                best_result = RunResult(
                    run_id=run_id,
                    request=request,
                    snapshot=runtime_snapshot,
                    dag=dag,
                    evaluation=evaluation,
                    reflection=reflection,
                    execution_trace=execution_trace,
                    workload_trace=workload_trace,
                    output_dir=str(output_dir),
                    rounds=round_no,
                )
                self._write_round_artifacts(
                    round_dir, request, runtime_snapshot, dag, execution_trace,
                    evaluation, reflection, react_trace, safety, workload_trace,
                )
                self._write_reflection_comparison(output_dir)
        finally:
            workload_trace["events"].append(runner.stop())
            workload_trace["status"] = runner.status()
            self._write_json(round_dir / "workload_trace.json", workload_trace)

        best_result.workload_trace = workload_trace
        if should_stop:
            self._write_report(best_result)
        return best_result, reflection, should_stop

    def _assert_workload_running(
        self,
        runner: Any,
        phase: str,
        round_dir: Path,
        workload_trace: dict[str, Any],
    ) -> None:
        status = runner.status()
        if status.get("running") is not False:
            return
        event = {
            "event": "workload_exited_before_phase10",
            "phase": phase,
            "timestamp": time.time(),
            "status": status,
        }
        workload_trace.setdefault("events", []).append(event)
        workload_trace["status"] = status
        self._write_json(round_dir / "workload_trace.json", workload_trace)
        raise RuntimeError(
            "Background workload exited before Phase 10 "
            f"during {phase}: exit_code={status.get('exit_code')}, "
            f"runtime_config_path={status.get('runtime_config_path', '')}, "
            f"stdout_tail={status.get('stdout_tail', '')!r}, "
            f"stderr_tail={status.get('stderr_tail', '')!r}"
        )

    def _assert_execution_success(
        self,
        execution_trace: dict[str, Any] | None,
        round_dir: Path,
        workload_trace: dict[str, Any],
    ) -> None:
        trace = execution_trace or {}
        failed_tasks = []
        for task_id, task in (trace.get("tasks") or {}).items():
            status = task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
            if status == "failed":
                failed_tasks.append({
                    "task_id": task_id,
                    "status": status,
                    "stderr": task.get("stderr", "") if isinstance(task, dict) else getattr(task, "stderr", ""),
                    "errors": task.get("errors", []) if isinstance(task, dict) else getattr(task, "errors", []),
                })
        cleanup_status = trace.get("cleanup_status") if isinstance(trace, dict) else ""
        if cleanup_status == "unknown" and trace.get("cleanup_errors"):
            failed_tasks.append({
                "task_id": "<executor>",
                "status": cleanup_status,
                "stderr": "",
                "errors": trace.get("cleanup_errors", []),
            })
        if not failed_tasks:
            return
        event = {
            "event": "execution_failed_before_evaluation",
            "timestamp": time.time(),
            "failed_tasks": failed_tasks,
        }
        workload_trace.setdefault("events", []).append(event)
        self._write_json(round_dir / "execution_trace.json", trace)
        self._write_json(round_dir / "workload_trace.json", workload_trace)
        details = "; ".join(
            f"{t['task_id']} status={t['status']} errors={t.get('errors') or t.get('stderr')}"
            for t in failed_tasks
        )
        raise RuntimeError(f"Anomaly injection failed before evaluation: {details}")

    def _validate_workload_timing_before_start(
        self,
        request: ExperimentRequest,
        workload_cfg: dict[str, Any],
        round_dir: Path,
    ) -> None:
        planning_budget_sec = self._planning_budget_sec()
        inspect_safety_margin_sec = self._inspect_safety_margin_sec()
        required = self._workload_required_sec(request, workload_cfg)
        payload = {
            "status": "passed",
            "phase": "before_start_workload",
            "warmup_sec": workload_cfg.get("warmup_sec", 0),
            "baseline_sec": workload_cfg.get("baseline_sec", 0),
            "injection_observe_sec": workload_cfg.get("injection_observe_sec", 0),
            "recovery_sec": workload_cfg.get("recovery_sec", 0),
            "request_max_duration_sec": request.max_duration_sec,
            "executor_grace_sec": EXECUTOR_GRACE_SEC,
            "planning_budget_sec": planning_budget_sec,
            "inspect_safety_margin_sec": inspect_safety_margin_sec,
            "workload_required_sec": required,
            "configured_workload_duration_sec": workload_cfg.get("duration_sec"),
        }
        duration = workload_cfg.get("duration_sec")
        if duration is not None and float(duration) < required:
            payload["status"] = "failed"
            payload["reason"] = (
                f"workload.duration_sec {float(duration)}s is shorter than required "
                f"single-round budget {required}s"
            )
            self._write_json(round_dir / "workload_timing_validation.json", payload)
            raise RuntimeError(payload["reason"])
        self._write_json(round_dir / "workload_timing_validation.json", payload)

    @staticmethod
    def _planning_budget_sec() -> float:
        return 2 * 120.0 + 30.0

    @staticmethod
    def _inspect_safety_margin_sec() -> float:
        return 30.0

    def _workload_required_sec(self, request: ExperimentRequest, workload_cfg: dict[str, Any]) -> float:
        return (
            float(workload_cfg.get("warmup_sec", 0) or 0)
            + float(workload_cfg.get("baseline_sec", 0) or 0)
            + float(request.max_duration_sec)
            + EXECUTOR_GRACE_SEC
            + float(workload_cfg.get("recovery_sec", 0) or 0)
            + self._planning_budget_sec()
            + self._inspect_safety_margin_sec()
        )

    def cleanup(self, run_id: str, output_root: str = "experiment_runs") -> dict[str, Any]:
        """
        Run cleanup for a previous experiment run.

        Destroys any ChaosBlade UIDs and marks tasks as cleaned.
        """
        run_dir = Path(output_root) / run_id
        if not run_dir.exists():
            return {"ok": False, "error": f"Run directory not found: {run_dir}"}

        errors: list[str] = []

        # Destroy chaosblade UIDs if recorded
        try:
            uid_file = run_dir / "chaosblade_uids.json"
            if uid_file.exists():
                uids = json.loads(uid_file.read_text())
                for uid in uids:
                    try:
                        import subprocess
                        result = subprocess.run(
                            [self.config.chaosblade_path, "destroy", uid],
                            capture_output=True, text=True, timeout=10,
                        )
                        if result.returncode != 0:
                            errors.append(f"blade destroy {uid} returned {result.returncode}")
                    except Exception as exc:
                        errors.append(f"Failed to destroy {uid}: {exc}")
        except Exception as exc:
            errors.append(f"Cleanup error: {exc}")

        return {"ok": len(errors) == 0, "errors": errors}

    # -------------------------------------------------------------------------
    # Step implementations
    # -------------------------------------------------------------------------

    def _inspect(
        self,
        request: ExperimentRequest,
        round_no: int,
        round_dir: Path,
    ) -> EnvironmentSnapshot:
        return self._inspect_named(request, round_no, round_dir, "snapshot.json")

    def _inspect_named(
        self,
        request: ExperimentRequest,
        round_no: int,
        round_dir: Path,
        filename: str,
    ) -> EnvironmentSnapshot:
        snapshot = self.planner.inspect(request)
        (round_dir / filename).write_text(
            json.dumps(to_jsonable(snapshot), indent=2, ensure_ascii=False)
        )
        return snapshot

    def _sleep_phase(self, seconds: float, phase: str, workload_trace: dict[str, Any]) -> None:
        workload_trace.setdefault("events", []).append(
            {"event": phase, "timestamp": time.time(), "duration_sec": seconds}
        )
        if seconds > 0:
            time.sleep(seconds)

    def _make_workload_runner(self, request: ExperimentRequest, round_dir: Path):
        return make_workload_runner(self.config, request.workload, round_dir)

    def _make_metrics_collector(self, request: ExperimentRequest, runner):
        return make_metrics_collector(self.config, request.workload, runner)

    def _plan(
        self,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        round_no: int,
        round_dir: Path,
        reflection: ReflectionResult | None = None,
    ) -> tuple[ExecutableTaskDAG, EnvironmentSnapshot, list[ReActStep]]:
        # Read memory for this anomaly
        memory_items = self.memory.load(anomaly=request.target_anomaly, limit=20)

        try:
            dag, snapshot, react_trace = self.planner.plan(request, snapshot, memory_items, reflection=reflection)
        except PlannerFallbackError as exc:
            self._write_planner_failure(round_dir, request, exc)
            raise RuntimeError(f"Planner fallback blocked: {exc.reason}") from exc

        # Write plan artifacts
        plan_payload = dict(self.planner.last_plan_payload)
        plan_payload.setdefault("target_path", request.target_path)
        plan_payload.setdefault("injected_nodes", request.injected_nodes)
        plan_payload["react_trace"] = [s.to_dict() for s in react_trace]
        (round_dir / "plan.json").write_text(json.dumps(plan_payload, indent=2, ensure_ascii=False))
        (round_dir / "react_trace.json").write_text(
            json.dumps([s.to_dict() for s in react_trace], indent=2, ensure_ascii=False)
        )
        self._validate_benchbase_burst_windows(request, dag, round_dir)
        return dag, snapshot, react_trace

    def _safety_check(
        self,
        dag: ExecutableTaskDAG,
        snapshot: EnvironmentSnapshot,
        round_dir: Path,
        request: ExperimentRequest | None = None,
    ) -> SafetyResult:
        # Convert DAG to dict for safety checker
        dag_dict = {
            "tasks": {tid: to_jsonable(t) for tid, t in dag.tasks.items()},
            "edges": [to_jsonable(e) for e in dag.edges],
            "schedule": dag.schedule,
        }
        result = self.safety_checker.check(
            task_dag=dag_dict,
            current_db_metrics=snapshot.db_metrics,
            current_os_metrics=snapshot.os_metrics,
            max_duration_sec=request.max_duration_sec if request else None,
            injection_observe_sec=(
                normalize_workload_config(request.workload, request.target_database)["injection_observe_sec"]
                if request and request.workload.get("enabled")
                else None
            ),
            expected_workload=(
                normalize_workload_config(request.workload, request.target_database)
                if request and request.workload.get("enabled")
                else None
            ),
        )
        (round_dir / "safety.json").write_text(
            json.dumps(to_jsonable(result), indent=2, ensure_ascii=False)
        )
        return result

    def _validate_benchbase_burst_windows(
        self,
        request: ExperimentRequest,
        dag: ExecutableTaskDAG,
        round_dir: Path,
    ) -> None:
        if not request.workload.get("enabled"):
            return
        workload_cfg = normalize_workload_config(request.workload, request.target_database)
        failures: list[str] = []
        dag_dict = {
            "tasks": {tid: to_jsonable(t) for tid, t in dag.tasks.items()},
            "edges": [to_jsonable(e) for e in dag.edges],
            "schedule": dag.schedule,
        }
        dag_required_sec, timing_reasons = estimate_dag_required_sec(
            dag_dict,
            include_grace=False,
            reject_workload_ramp=True,
        )
        failures.extend(timing_reasons)
        if dag_required_sec > float(workload_cfg["injection_observe_sec"]):
            failures.append(
                f"DAG required duration {dag_required_sec}s exceeds injection_observe_sec "
                f"{workload_cfg['injection_observe_sec']}s"
            )
        if dag_required_sec > float(request.max_duration_sec):
            failures.append(
                f"DAG required duration {dag_required_sec}s exceeds request max_duration_sec "
                f"{request.max_duration_sec}s"
            )
        for task_id, task in dag.tasks.items():
            for action in task.actions or []:
                if action.get("kind") != "benchbase_burst":
                    continue
                profile = action.get("profile") or {}
                duration = float(profile.get("duration_sec", action.get("duration_sec", 0)) or 0)
                benchmark = str(profile.get("benchmark") or "").lower()
                if benchmark != str(workload_cfg.get("benchmark") or "").lower():
                    failures.append(
                        f"{task_id}: benchbase_burst benchmark {benchmark} does not match workload benchmark {workload_cfg.get('benchmark')}"
                    )
                if str(profile.get("database") or "") != str(workload_cfg.get("database") or ""):
                    failures.append(
                        f"{task_id}: benchbase_burst database {profile.get('database')} does not match workload database {workload_cfg.get('database')}"
                    )
                if str(profile.get("config_path") or "") != str(workload_cfg.get("config_path") or ""):
                    failures.append(f"{task_id}: benchbase_burst config_path does not match workload config_path")
        payload = {
            "status": "passed",
            "failure_type": "",
            "reasons": [],
            "target_path": request.target_path,
            "injected_nodes": request.injected_nodes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "warmup_sec": workload_cfg.get("warmup_sec", 0),
            "baseline_sec": workload_cfg.get("baseline_sec", 0),
            "injection_observe_sec": workload_cfg.get("injection_observe_sec", 0),
            "recovery_sec": workload_cfg.get("recovery_sec", 0),
            "request_max_duration_sec": request.max_duration_sec,
            "executor_grace_sec": EXECUTOR_GRACE_SEC,
            "dag_required_sec": dag_required_sec,
            "workload_required_sec": None,
        }
        if failures:
            payload["status"] = "failed"
            payload["failure_type"] = "phase_timing_validation"
            payload["reasons"] = failures
            self._write_json(round_dir / "plan_validation_failure.json", payload)
            self._write_json(round_dir / "timing_validation.json", payload)
            raise RuntimeError("Timing validation failed: " + "; ".join(failures))
        self._write_json(round_dir / "timing_validation.json", payload)

    def _collect_baseline(self, request: ExperimentRequest, round_dir: Path) -> dict[str, Any]:
        baseline = tool_registry.collect_baseline_metrics(
            config=self.config,
            database=request.target_database,
        )
        (round_dir / "baseline_metrics.json").write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False)
        )
        return baseline

    def _execute(
        self,
        dag: ExecutableTaskDAG,
        round_no: int,
        round_dir: Path,
        request: ExperimentRequest,
    ) -> dict[str, Any]:
        dag_dict = {
            "tasks": {tid: to_jsonable(t) for tid, t in dag.tasks.items()},
            "edges": [to_jsonable(e) for e in dag.edges],
            "schedule": dag.schedule,
        }
        trace = tool_registry.execute_dag(
            task_dag=dag_dict,
            config=self.config,
            max_duration_sec=request.max_duration_sec,
            round_dir=str(round_dir),
        )
        (round_dir / "execution_trace.json").write_text(
            json.dumps(trace, indent=2, ensure_ascii=False)
        )
        return trace

    def _collect_after(self, request: ExperimentRequest, round_dir: Path) -> dict[str, Any]:
        after = tool_registry.collect_baseline_metrics(
            config=self.config,
            database=request.target_database,
        )
        (round_dir / "post_metrics.json").write_text(
            json.dumps(after, indent=2, ensure_ascii=False)
        )
        return after

    def _start_slow_log_capture(self, round_dir: Path) -> tuple[SlowLogProbe, dict[str, Any]]:
        probe = SlowLogProbe(self.config)
        marker = probe.start_capture()
        self._write_json(round_dir / "slow_log_marker.json", marker)
        return probe, marker

    def _finish_slow_log_capture(
        self,
        probe: SlowLogProbe,
        marker: dict[str, Any],
        target_database: str,
        round_dir: Path,
    ) -> dict[str, Any]:
        try:
            evidence = probe.collect(marker, target_database=target_database)
        except Exception as exc:
            evidence = {
                "available": False,
                "source": "none",
                "entries": [],
                "entry_count": 0,
                "target_database": target_database,
                "target_entries": [],
                "target_entry_count": 0,
                "matched": False,
                "variables_at_injection_start": marker.get("variables_at_injection_start") or {},
                "variables_at_injection_end": {},
                "error": str(exc),
            }
        finally:
            restore = probe.restore(marker)
        evidence["restore"] = restore
        self._write_json(round_dir / "slow_log_evidence.json", evidence)
        return evidence

    def _evaluate(
        self,
        request: ExperimentRequest,
        baseline: dict[str, Any],
        after: dict[str, Any],
        dag: ExecutableTaskDAG,
        target_path: list[str],
        execution_trace: dict[str, Any],
        round_dir: Path,
    ) -> EvaluationResult:
        # Infer target path from the first injectable node to a terminal node
        result = evaluate(
            baseline=baseline,
            after=after,
            target_path=target_path,
            execution_trace=execution_trace,
        )
        (round_dir / "evaluation_result.json").write_text(
            json.dumps(to_jsonable(result), indent=2, ensure_ascii=False)
        )
        return result

    def _reflect(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        round_no: int,
        round_dir: Path,
    ) -> ReflectionResult:
        memory_items = self.memory.load(anomaly=request.target_anomaly, limit=20)
        try:
            reflection = self.planner.reflect(evaluation, request, memory_items)
        except ReflectionFallbackError as exc:
            self._write_reflection_failure(round_dir, request, evaluation, exc)
            raise RuntimeError(f"Reflection fallback blocked: {exc.reason}") from exc
        (round_dir / "reflection_result.json").write_text(
            json.dumps(to_jsonable(reflection), indent=2, ensure_ascii=False)
        )
        return reflection

    def _write_planner_failure(
        self,
        round_dir: Path,
        request: ExperimentRequest,
        exc: PlannerFallbackError,
    ) -> None:
        trace = exc.trace or []
        if not trace and getattr(self.planner, "_react_trace", None):
            trace = [s.to_dict() for s in self.planner._react_trace]
        payload = {
            "status": "failed",
            "failure_type": "planner_fallback_blocked",
            "reason": exc.reason,
            "target_path": request.target_path,
            "injected_nodes": request.injected_nodes,
            "react_trace": trace,
            "context": exc.context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(round_dir / "planner_failure.json", payload)
        self._write_json(round_dir / "react_trace.json", trace)
        self._write_json(round_dir / "plan.json", {
            "planning_status": "failed",
            "failure_type": "planner_fallback_blocked",
            "reason": exc.reason,
            "target_path": request.target_path,
            "injected_nodes": request.injected_nodes,
            "fallback_used": False,
            "fallback_blocked": True,
            "react_trace": trace,
        })

    def _write_reflection_failure(
        self,
        round_dir: Path,
        request: ExperimentRequest,
        evaluation: EvaluationResult,
        exc: ReflectionFallbackError,
    ) -> None:
        self._write_json(round_dir / "reflection_failure.json", {
            "status": "failed",
            "failure_type": "reflection_fallback_blocked",
            "reason": exc.reason,
            "target_path": request.target_path,
            "injected_nodes": request.injected_nodes,
            "evaluation": to_jsonable(evaluation),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _update_memory(
        self,
        evaluation: EvaluationResult,
        request: ExperimentRequest,
        reflection: ReflectionResult,
        round_no: int,
    ) -> None:
        path_result = evaluation.path_result
        target_path = path_result.target_path if path_result else []
        node_hit_ratio = path_result.node_hit_ratio if path_result else 0.0

        # Extract task parameters from failed nodes
        task_params: dict[str, Any] = {}
        for node_id in evaluation.failed_nodes:
            nr = evaluation.node_results.get(node_id)
            if nr:
                task_params[node_id] = {
                    "hit": nr.hit if hasattr(nr, "hit") else nr.get("hit"),
                    "confidence": nr.confidence if hasattr(nr, "confidence") else nr.get("confidence", 0.0),
                }

        self.memory.append_reflection(
            anomaly=request.target_anomaly,
            path=target_path,
            task_params=task_params,
            outcome=evaluation.reason,
            success=evaluation.success,
            round_no=round_no,
            node_hit_ratio=node_hit_ratio,
        )

    # -------------------------------------------------------------------------
    # Artifact writing
    # -------------------------------------------------------------------------

    def _write_round_observation_summary(
        self,
        round_dir: Path,
        round_no: int,
        baseline_window: dict[str, Any] | None,
        injection_window: dict[str, Any] | None,
        recovery_window: dict[str, Any] | None,
        execution_trace: dict[str, Any] | None,
        slow_log_evidence: dict[str, Any] | None,
        evaluation: EvaluationResult | None,
    ) -> dict[str, Any]:
        summary = {
            "round": round_no,
            "score": evaluation.final_score if evaluation else None,
            "success": evaluation.success if evaluation else None,
            "node_hits": _node_hits(evaluation),
            "phases": {
                "baseline": _phase_observation(baseline_window),
                "injection": _phase_observation(injection_window),
                "recovery": _phase_observation(recovery_window),
            },
            "raw_sql_workload": _raw_sql_latency_summaries(execution_trace or {}),
            "slow_log": _slow_log_summary(slow_log_evidence or {}),
        }
        self._write_json(round_dir / "round_observation_summary.json", summary)
        return summary

    def _write_reflection_comparison(self, output_dir: Path) -> dict[str, Any]:
        rounds: list[dict[str, Any]] = []
        for round_dir in sorted(output_dir.glob("round_*")):
            obs_path = round_dir / "round_observation_summary.json"
            eval_path = round_dir / "evaluation_result.json"
            if not obs_path.exists():
                continue
            try:
                obs = json.loads(obs_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if eval_path.exists():
                try:
                    ev = json.loads(eval_path.read_text(encoding="utf-8"))
                    obs.setdefault("score", ev.get("final_score"))
                    obs.setdefault("success", ev.get("success"))
                    obs.setdefault("node_hits", _node_hits_from_payload(ev))
                except Exception:
                    pass
            rounds.append(obs)

        comparison: dict[str, Any] = {
            "available": len(rounds) >= 2,
            "round_count": len(rounds),
            "rounds": rounds,
        }
        if len(rounds) >= 2:
            before = rounds[0]
            after = rounds[1]
            comparison.update({
                "score_delta": _number(after.get("score")) - _number(before.get("score")),
                "node_hit_changes": _compare_node_hits(
                    before.get("node_hits") or {},
                    after.get("node_hits") or {},
                ),
                "qps": {
                    "baseline": _compare_phase_qps(before, after, "baseline"),
                    "injection": _compare_phase_qps(before, after, "injection"),
                    "recovery": _compare_phase_qps(before, after, "recovery"),
                },
                "query_latency": {
                    "baseline": _compare_phase_latency(before, after, "baseline"),
                    "injection": _compare_phase_latency(before, after, "injection"),
                    "recovery": _compare_phase_latency(before, after, "recovery"),
                },
                "raw_sql_workload": _compare_raw_sql(before, after),
            })
        self._write_json(output_dir / "reflection_comparison.json", comparison)
        return comparison

    def _write_round_artifacts(
        self,
        round_dir: Path,
        request: ExperimentRequest,
        snapshot: EnvironmentSnapshot,
        dag: ExecutableTaskDAG,
        execution_trace: dict[str, Any] | None,
        evaluation: EvaluationResult,
        reflection: ReflectionResult | None,
        react_trace: list[ReActStep],
        safety: SafetyResult,
        workload_trace: dict[str, Any] | None = None,
    ) -> None:
        dag_dict = {
            "tasks": {tid: to_jsonable(t) for tid, t in dag.tasks.items()},
            "edges": [to_jsonable(e) for e in dag.edges],
            "schedule": dag.schedule,
        }
        self._write_json(round_dir / "request.json", to_jsonable(request))
        self._write_json(round_dir / "snapshot.json", to_jsonable(snapshot))
        self._write_json(round_dir / "task_dag.json", dag_dict)
        if execution_trace:
            self._write_json(round_dir / "execution_trace.json", execution_trace)
        self._write_json(round_dir / "evaluation_result.json", to_jsonable(evaluation))
        if reflection:
            self._write_json(round_dir / "reflection_result.json", to_jsonable(reflection))
        self._write_json(round_dir / "safety.json", to_jsonable(safety))
        if workload_trace is not None:
            self._write_json(round_dir / "workload_trace.json", workload_trace)

    def _write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _write_run_identity(self, output_dir: Path, run_id: str, request: ExperimentRequest) -> None:
        identity = {
            "run_id": run_id,
            "input": request.source_path,
            "target_anomaly": request.target_anomaly,
            "target_database": request.target_database,
            "target_path": request.target_path,
            "target_path_label": " -> ".join(request.target_path),
            "injected_nodes": request.injected_nodes,
            "dba_description": request.dba_description,
        }
        self._write_json(output_dir / "run_identity.json", identity)

    def _write_report(self, result: RunResult) -> None:
        from agent.report import write_report
        write_report(result, Path(result.output_dir) / "report.md")

    # -------------------------------------------------------------------------
    # Standalone experiment API
    # -------------------------------------------------------------------------

    def generate_tasks_only(
        self,
        request: ExperimentRequest,
        output_root: str = "experiment_runs/taskgen",
        workload_mode: str = "auto",
        taskgen_probe_interval_sec: float = 3.0,
        qps_threshold: float = 0.1,
    ) -> dict[str, Any]:
        """Start background workload, generate TaskSpecs/DAG, write artifacts, and stop."""
        workload_mode = str(workload_mode or "auto").lower()
        if workload_mode not in {"auto", "start", "reuse", "none"}:
            raise ValueError(f"unsupported taskgen workload_mode: {workload_mode}")
        if workload_mode != "none" and not request.workload.get("enabled"):
            raise ValueError("taskgen requires request.workload.enabled=true")

        run_id = _generate_run_id()
        output_dir = Path(output_root) / run_id
        round_dir = output_dir / "round_1"
        round_dir.mkdir(parents=True, exist_ok=True)

        workload_cfg = normalize_workload_config(request.workload, request.target_database)
        workload_cfg["jar_path"] = self.config.benchbase_jar_path
        workload_cfg["java_bin"] = self.config.benchbase_java_bin
        request.workload = dict(workload_cfg)
        self._write_json(round_dir / "request.json", to_jsonable(request))
        self._write_json(round_dir / "workload_config.json", workload_cfg)

        workload_trace: dict[str, Any] = {"config": workload_cfg, "events": [], "samples": [], "source": "none"}
        runner = None
        started_by_taskgen = False
        dag = ExecutableTaskDAG()
        snapshot = EnvironmentSnapshot(database=request.target_database)
        react_trace: list[ReActStep] = []
        detection = self._detect_taskgen_workload(
            request=request,
            mode=workload_mode,
            interval_sec=taskgen_probe_interval_sec,
            qps_threshold=qps_threshold,
        )
        self._write_json(round_dir / "taskgen_workload_detection.json", detection)

        try:
            if workload_mode == "reuse" and not detection["existing_workload_detected"]:
                raise RuntimeError(
                    "taskgen workload_mode=reuse requires an existing workload, "
                    f"but detected qps={detection['qps']}, tps={detection['tps']}"
                )
            if workload_mode in {"auto", "start"} and (
                workload_mode == "start" or not detection["existing_workload_detected"]
            ):
                runner = self._make_workload_runner(request, round_dir)
                workload_trace["source"] = "started_by_taskgen"
                workload_trace["events"].append(runner.start())
                detection["started_new_workload"] = True
                self._write_json(round_dir / "taskgen_workload_detection.json", detection)
                self._write_json(round_dir / "workload_trace.json", workload_trace)
                self._assert_workload_running(runner, "taskgen_after_start_workload", round_dir, workload_trace)
                started_by_taskgen = True
            elif detection["existing_workload_detected"]:
                workload_trace["source"] = "existing"
                workload_trace["events"].append({
                    "event": "reuse_existing_workload",
                    "timestamp": time.time(),
                    "qps": detection["qps"],
                    "tps": detection["tps"],
                })
                self._write_json(round_dir / "workload_trace.json", workload_trace)
            else:
                workload_trace["source"] = "none"
                workload_trace["events"].append({"event": "no_workload_started", "timestamp": time.time()})
                self._write_json(round_dir / "workload_trace.json", workload_trace)

            if started_by_taskgen and runner is not None:
                self._assert_workload_running(runner, "taskgen_before_inspect", round_dir, workload_trace)
            snapshot = self.planner.inspect(request)
            if started_by_taskgen and runner is not None:
                self._assert_workload_running(runner, "taskgen_after_inspect_before_plan", round_dir, workload_trace)
                snapshot.workload_status = {
                    "phase": "taskgen_after_workload_start",
                    "status": runner.status(),
                }
            elif detection["existing_workload_detected"]:
                snapshot.workload_status = {
                    "phase": "taskgen_reuse_existing_workload",
                    "detected_qps": detection["qps"],
                    "detected_tps": detection["tps"],
                    "probe_interval_sec": detection["probe_interval_sec"],
                }
            else:
                snapshot.workload_status = {
                    "phase": "taskgen_no_workload",
                    "detected_qps": detection["qps"],
                    "detected_tps": detection["tps"],
                }
            self._write_json(round_dir / "static_snapshot.json", to_jsonable(snapshot))
            self._write_json(round_dir / "snapshot.json", to_jsonable(snapshot))

            memory_items = self.memory.load(anomaly=request.target_anomaly, limit=20)
            try:
                if started_by_taskgen and runner is not None:
                    self._assert_workload_running(runner, "taskgen_before_planning", round_dir, workload_trace)
                dag, snapshot, react_trace = self.planner.plan(request, snapshot, memory_items)
                if started_by_taskgen and runner is not None:
                    self._assert_workload_running(runner, "taskgen_after_planning_before_write_artifacts", round_dir, workload_trace)
            except PlannerFallbackError as exc:
                self._write_planner_failure(round_dir, request, exc)
                raise RuntimeError(f"Planner fallback blocked: {exc.reason}") from exc

            plan_payload = dict(self.planner.last_plan_payload)
            plan_payload.setdefault("target_path", request.target_path)
            plan_payload.setdefault("injected_nodes", request.injected_nodes)
            plan_payload["react_trace"] = [s.to_dict() for s in react_trace]
            dag_dict = {
                "tasks": {tid: to_jsonable(t) for tid, t in dag.tasks.items()},
                "edges": [to_jsonable(e) for e in dag.edges],
                "schedule": dag.schedule,
            }
            generated = {
                "run_id": run_id,
                "target_path": request.target_path,
                "injected_nodes": request.injected_nodes,
                "workload": workload_cfg,
                "task_specs": [to_jsonable(t) for t in dag.tasks.values()],
                "dependencies": [[edge.source, edge.target] for edge in dag.edges],
                "dag": dag_dict,
                "workload_detection": detection,
                "react_trace_path": "react_trace.json",
                "plan_path": "plan.json",
                "task_dag_path": "task_dag.json",
            }
            self._write_json(round_dir / "plan.json", plan_payload)
            self._write_json(round_dir / "react_trace.json", [s.to_dict() for s in react_trace])
            self._write_json(round_dir / "task_dag.json", dag_dict)
            self._write_json(round_dir / "generated_tasks.json", generated)
            return {
                "run_id": run_id,
                "output_dir": str(output_dir),
                "round_dir": str(round_dir),
                "task_specs": generated["task_specs"],
                "dag": dag_dict,
            }
        finally:
            if started_by_taskgen and runner is not None:
                workload_trace["events"].append(runner.stop())
                workload_trace["status"] = runner.status()
            self._write_json(round_dir / "workload_trace.json", workload_trace)

    def _detect_taskgen_workload(
        self,
        request: ExperimentRequest,
        mode: str,
        interval_sec: float,
        qps_threshold: float,
    ) -> dict[str, Any]:
        if mode in {"start", "none"}:
            return {
                "mode": mode,
                "probe_interval_sec": 0,
                "qps": 0.0,
                "tps": 0.0,
                "existing_workload_detected": False,
                "started_new_workload": False,
                "skipped_probe": True,
            }
        probe = MySQLProbe(
            database=request.target_database,
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
        )
        metrics = probe.workload_probe(interval_sec=float(interval_sec))
        qps = float(metrics.get("qps") or 0.0)
        tps = float(metrics.get("tps") or 0.0)
        return {
            "mode": mode,
            "probe_interval_sec": float(interval_sec),
            "qps": qps,
            "tps": tps,
            "existing_workload_detected": qps > float(qps_threshold),
            "started_new_workload": False,
            "qps_threshold": float(qps_threshold),
        }

    def plan_only(self, request: ExperimentRequest) -> tuple[ExecutableTaskDAG, EnvironmentSnapshot]:
        """Plan without executing (for dry-run / review)."""
        snapshot = self.inspect(request)
        memory_items = self.memory.load(anomaly=request.target_anomaly, limit=20)
        dag, snapshot, _ = self.planner.plan(request, snapshot, memory_items)
        return dag, snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phase_observation(window: dict[str, Any] | None) -> dict[str, Any]:
    if not window:
        return {
            "available": False,
            "qps": {},
            "query_latency_top10": [],
            "query_latency_overall": {},
        }
    summary = window.get("summary") or {}
    workload = summary.get("workload") or window.get("workload") or {}
    qps = workload.get("qps") or {}
    return {
        "available": True,
        "duration_sec": window.get("duration_sec"),
        "sample_count": window.get("sample_count"),
        "qps": qps if isinstance(qps, dict) else {"avg": qps},
        "tps": (workload.get("tps") if isinstance(workload.get("tps"), dict) else {"avg": workload.get("tps")}),
        "query_latency_top10": summary.get("query_latency_top10") or [],
        "query_latency_overall": summary.get("query_latency_overall") or {},
        "slow_log_evidence": summary.get("performance_schema_slow_sql") or {},
    }


def _raw_sql_latency_summaries(execution_trace: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for task_id, task in (execution_trace.get("tasks") or {}).items():
        metrics = task.get("metrics", {}) if isinstance(task, dict) else getattr(task, "metrics", {})
        for action in metrics.get("actions", []) or []:
            result = action.get("result") or {}
            if not isinstance(result, dict):
                continue
            if result.get("kind") not in {"raw_sql_workload", "sql_workload"}:
                continue
            summary = {
                "task_id": task_id,
                "kind": result.get("kind"),
                "database": result.get("database"),
                "executions": result.get("executions"),
                "error_count": result.get("error_count"),
                "slow_threshold_ms": result.get("slow_threshold_ms"),
                "avg_ms": result.get("avg_ms"),
                "median_ms": result.get("median_ms"),
                "p95_ms": result.get("p95_ms"),
                "max_ms": result.get("max_ms"),
                "top10_slowest_ms": result.get("top10_slowest_ms") or [],
                "above_long_query_time_count": result.get("above_long_query_time_count"),
                "latency_artifact": result.get("latency_artifact"),
            }
            artifact = result.get("latency_artifact")
            if artifact:
                try:
                    payload = json.loads(Path(str(artifact)).read_text(encoding="utf-8"))
                    for key in (
                        "avg_ms",
                        "median_ms",
                        "p95_ms",
                        "max_ms",
                        "top10_slowest_ms",
                        "above_long_query_time_count",
                    ):
                        if summary.get(key) in (None, []):
                            summary[key] = payload.get(key)
                except Exception:
                    pass
            summaries.append(summary)
    return summaries


def _slow_log_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    entries = evidence.get("target_entries") or []
    return {
        "available": evidence.get("available", False),
        "source": evidence.get("source"),
        "target_database": evidence.get("target_database"),
        "target_entry_count": evidence.get("target_entry_count", len(entries)),
        "matched": evidence.get("matched", False),
        "long_query_time": (
            (evidence.get("variables_at_injection_end") or {}).get("long_query_time")
            or (evidence.get("variables_at_injection_start") or {}).get("long_query_time")
        ),
        "log_queries_not_using_indexes": (
            (evidence.get("variables_at_injection_end") or {}).get("log_queries_not_using_indexes")
            or (evidence.get("variables_at_injection_start") or {}).get("log_queries_not_using_indexes")
        ),
        "entries": entries,
    }


def _node_hits(evaluation: EvaluationResult | None) -> dict[str, bool]:
    if evaluation is None:
        return {}
    return {
        node_id: bool(result.hit if hasattr(result, "hit") else result.get("hit"))
        for node_id, result in (evaluation.node_results or {}).items()
    }


def _node_hits_from_payload(payload: dict[str, Any]) -> dict[str, bool]:
    node_results = payload.get("node_results") or {}
    return {
        node_id: bool(result.get("hit"))
        for node_id, result in node_results.items()
        if isinstance(result, dict)
    }


def _compare_node_hits(before: dict[str, bool], after: dict[str, bool]) -> dict[str, dict[str, bool]]:
    changes = {}
    for node_id in sorted(set(before) | set(after)):
        b = bool(before.get(node_id))
        a = bool(after.get(node_id))
        if b != a:
            changes[node_id] = {"before": b, "after": a}
    return changes


def _compare_phase_qps(before: dict[str, Any], after: dict[str, Any], phase: str) -> dict[str, Any]:
    before_qps = (((before.get("phases") or {}).get(phase) or {}).get("qps") or {})
    after_qps = (((after.get("phases") or {}).get(phase) or {}).get("qps") or {})
    return {
        "before_avg": _number(before_qps.get("avg")),
        "after_avg": _number(after_qps.get("avg")),
        "delta": _number(after_qps.get("avg")) - _number(before_qps.get("avg")),
        "before": before_qps,
        "after": after_qps,
    }


def _compare_phase_latency(before: dict[str, Any], after: dict[str, Any], phase: str) -> dict[str, Any]:
    before_phase = ((before.get("phases") or {}).get(phase) or {})
    after_phase = ((after.get("phases") or {}).get(phase) or {})
    before_overall = before_phase.get("query_latency_overall") or {}
    after_overall = after_phase.get("query_latency_overall") or {}
    return {
        "overall": {
            "before": before_overall,
            "after": after_overall,
            "avg_ms_delta": _number(after_overall.get("avg_latency_ms")) - _number(before_overall.get("avg_latency_ms")),
            "median_ms_delta": _number(after_overall.get("median_latency_ms")) - _number(before_overall.get("median_latency_ms")),
            "p95_ms_delta": _number(after_overall.get("p95_latency_ms")) - _number(before_overall.get("p95_latency_ms")),
            "max_ms_delta": _number(after_overall.get("max_latency_ms")) - _number(before_overall.get("max_latency_ms")),
        },
        "top10_before": before_phase.get("query_latency_top10") or [],
        "top10_after": after_phase.get("query_latency_top10") or [],
    }


def _compare_raw_sql(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_items = before.get("raw_sql_workload") or []
    after_items = after.get("raw_sql_workload") or []
    before_first = before_items[0] if before_items else {}
    after_first = after_items[0] if after_items else {}
    return {
        "before": before_items,
        "after": after_items,
        "avg_ms_delta": _number(after_first.get("avg_ms")) - _number(before_first.get("avg_ms")),
        "median_ms_delta": _number(after_first.get("median_ms")) - _number(before_first.get("median_ms")),
        "p95_ms_delta": _number(after_first.get("p95_ms")) - _number(before_first.get("p95_ms")),
        "max_ms_delta": _number(after_first.get("max_ms")) - _number(before_first.get("max_ms")),
    }


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _generate_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:6]}"
