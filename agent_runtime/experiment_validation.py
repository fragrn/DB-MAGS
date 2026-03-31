from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

from agent_runtime.runtime import RuntimeComponents, build_components
from agent_runtime.types import ExperimentRequest


def _to_jsonable(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(val) for key, val in value.items()}
    return value


class AgentValidationRunner:
    def __init__(self, components: RuntimeComponents | None = None):
        self.components = components or build_components()

    def run(self, output_root: Path, database: str) -> Dict[str, Any]:
        output_root.mkdir(parents=True, exist_ok=True)
        suite = {
            "generated_at": datetime.now(UTC).isoformat(),
            "database": database,
            "model": self.components.config.openai_model,
            "openai_available": self.components.llm_client.available(),
            "experiments": [],
        }
        for experiment_id, handler in self._experiments().items():
            result = handler(output_root / experiment_id, database)
            suite["experiments"].append(result)
        summary_path = output_root / "suite_summary.json"
        summary_path.write_text(json.dumps(_to_jsonable(suite), ensure_ascii=True, indent=2))
        return suite

    def _experiments(self) -> Dict[str, Callable[[Path, str], Dict[str, Any]]]:
        return {
            "E1_global": self._run_global,
            "E2_sql": self._run_sql,
            "E3_resource": self._run_resource,
            "E4_traffic": self._run_traffic,
            "E5_end_to_end": self._run_end_to_end,
        }

    def _run_global(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        planner = self.components.planner
        request_missing = ExperimentRequest(user_goal="Plan an anomaly validation run")
        context_missing = planner.gather_context(request_missing)
        response_missing = planner.plan(request_missing, context_missing)

        request = ExperimentRequest(
            user_goal="Plan missing index, cpu contention, and overall workload experiments",
            target_database=database,
            allowed_anomalies=["missing_index", "cpu", "overall_workload"],
            user_constraints={"baseline_sleep": 0.044, "baseline_threads": 300},
        )
        context = planner.gather_context(request)
        response = planner.plan(request, context)
        revised = planner.revise(request, "reduce the risk and keep only missing_index")
        revised_context = planner.gather_context(revised)
        revised_response = planner.plan(revised, revised_context)

        request_path = out_dir / "request.json"
        request_path.write_text(json.dumps(_to_jsonable({
            "missing_request": request_missing,
            "request": request,
            "revised_request": revised,
        }), ensure_ascii=True, indent=2))
        plan_path = out_dir / "plan.json"
        plan_path.write_text(json.dumps(_to_jsonable({
            "missing_response": response_missing,
            "response": response,
            "revised_response": revised_response,
        }), ensure_ascii=True, indent=2))

        passed = bool(response_missing.follow_up_questions) and response.plan is not None and revised_response.plan is not None
        observations = [
            f"follow_up_count={len(response_missing.follow_up_questions)}",
            f"initial_task_count={len(response.plan.tasks) if response.plan else 0}",
            f"revised_task_count={len(revised_response.plan.tasks) if revised_response.plan else 0}",
        ]
        result = {
            "status": "pass" if passed else "fail",
            "agent": "GlobalPlannerAgent",
            "input_summary": {
                "database": database,
                "allowed_anomalies": ["missing_index", "cpu", "overall_workload"],
                "openai_available": self.components.llm_client.available(),
            },
            "artifacts": {
                "request": str(request_path),
                "plan": str(plan_path),
            },
            "observations": observations,
            "failure_reason": None if passed else "planner did not produce expected follow-up and revised plans",
            "next_action": "Provide OpenAI credentials before executing live OpenAI-path validation." if not self.components.llm_client.available() else "Ready for live OpenAI-path execution.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_sql(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = ExperimentRequest(
            user_goal="Validate SQL anomaly generation for missing index",
            target_database=database,
            allowed_anomalies=["missing_index"],
        )
        context = self.components.planner.gather_context(request)
        sql_agent = self.components.planner.task_agents[0]
        tasks = sql_agent.prepare(context, request)
        task_path = out_dir / "task.json"
        task_path.write_text(json.dumps(_to_jsonable({"tasks": tasks}), ensure_ascii=True, indent=2))

        passed = self.components.llm_client.available() and len(tasks) >= 1
        failure_reason = None
        if not self.components.llm_client.available():
            failure_reason = "OpenAI API configuration required for the SQL agent experiment."
        elif not tasks:
            failure_reason = "No validated SQL task was produced."
        result = {
            "status": "pass" if passed else "fail",
            "agent": "SQLAnomalyAgent",
            "input_summary": {"database": database, "anomaly": "missing_index", "openai_available": self.components.llm_client.available()},
            "artifacts": {"task": str(task_path)},
            "observations": [f"task_count={len(tasks)}"],
            "failure_reason": failure_reason,
            "next_action": "Provide OPENAI_API_KEY and rerun this experiment." if failure_reason else "SQL agent produced at least one validated task.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_resource(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = ExperimentRequest(user_goal="Validate CPU contention command generation", target_database=database, allowed_anomalies=["cpu"])
        context = self.components.planner.gather_context(request)
        resource_agent = self.components.planner.task_agents[1]
        tasks = resource_agent.prepare(context, request)
        task_path = out_dir / "task.json"
        task_path.write_text(json.dumps(_to_jsonable({"tasks": tasks}), ensure_ascii=True, indent=2))
        command = tasks[0].execution_steps[0]["command"] if tasks else ""
        passed = bool(tasks) and ".tools/chaosblade-1.8.0-darwin_arm64/blade" in command
        result = {
            "status": "pass" if passed else "fail",
            "agent": "ResourceAgent",
            "input_summary": {"database": database, "anomaly": "cpu"},
            "artifacts": {"task": str(task_path)},
            "observations": [f"command={command}"],
            "failure_reason": None if passed else "resource command did not use the repo-scoped ChaosBlade binary",
            "next_action": "Resource command is ready for manual execution validation." if passed else "Inspect ChaosBlade path resolution.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_traffic(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = ExperimentRequest(
            user_goal="Validate workload surge planning",
            target_database=database,
            allowed_anomalies=["overall_workload"],
            user_constraints={"baseline_sleep": 0.044, "baseline_threads": 300},
        )
        context = self.components.planner.gather_context(request)
        traffic_agent = self.components.planner.task_agents[2]
        tasks = traffic_agent.prepare(context, request)
        task_path = out_dir / "task.json"
        task_path.write_text(json.dumps(_to_jsonable({"tasks": tasks}), ensure_ascii=True, indent=2))
        profile = tasks[0].inputs if tasks else {}
        passed = bool(tasks) and profile.get("sleep_time") is not None and profile.get("thread_count") is not None and profile.get("description")
        result = {
            "status": "pass" if passed else "fail",
            "agent": "TrafficAgent",
            "input_summary": {"database": database, "anomaly": "overall_workload", "baseline_sleep": 0.044, "baseline_threads": 300},
            "artifacts": {"task": str(task_path)},
            "observations": [json.dumps(profile, ensure_ascii=True)],
            "failure_reason": None if passed else "traffic task did not include the expected workload profile fields",
            "next_action": "Traffic profile is ready for workload integration." if passed else "Inspect workload_tuning_skill output.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_end_to_end(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        planner = self.components.planner
        request = ExperimentRequest(
            user_goal="Plan a combined validation run for missing_index, cpu, and overall_workload",
            target_database=database,
            allowed_anomalies=["missing_index", "cpu", "overall_workload"],
            user_constraints={"baseline_sleep": 0.044, "baseline_threads": 300},
        )
        context = planner.gather_context(request)
        initial = planner.plan(request, context)
        revised_request = planner.revise(request, "reduce the risk and keep only missing_index")
        revised_context = planner.gather_context(revised_request)
        revised = planner.plan(revised_request, revised_context)
        plan_path = out_dir / "plan.json"
        plan_path.write_text(json.dumps(_to_jsonable({"initial": initial, "revised": revised}), ensure_ascii=True, indent=2))

        initial_agents = {task.agent_type for task in (initial.plan.tasks if initial.plan else [])}
        revised_agents = {task.agent_type for task in (revised.plan.tasks if revised.plan else [])}
        passed = {"sql", "resource", "traffic"}.issubset(initial_agents) and "sql" in revised_agents
        result = {
            "status": "pass" if passed else "fail",
            "agent": "GlobalPlannerAgent+TaskAgents",
            "input_summary": {"database": database, "allowed_anomalies": ["missing_index", "cpu", "overall_workload"]},
            "artifacts": {"plan": str(plan_path)},
            "observations": [f"initial_agents={sorted(initial_agents)}", f"revised_agents={sorted(revised_agents)}"],
            "failure_reason": None if passed else "end-to-end planning did not fan out to the expected task agent mix",
            "next_action": "Ready for manual confirm-path testing in the CLI." if passed else "Inspect planner fan-out or revise behavior.",
        }
        self._write_result_files(out_dir, result)
        return result

    @staticmethod
    def _write_result_files(out_dir: Path, result: Dict[str, Any]) -> None:
        (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
        notes = [
            f"# {result['agent']} validation",
            "",
            f"status: {result['status']}",
            f"failure_reason: {result['failure_reason']}",
            f"next_action: {result['next_action']}",
        ]
        (out_dir / "notes.md").write_text("\n".join(notes) + "\n")
