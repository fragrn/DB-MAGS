from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict

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
            "E2_slow_sql": self._run_slow_sql,
            "E3_resource": self._run_resource,
            "E4_traffic": self._run_traffic,
            "E5_lock": self._run_lock,
            "E6_backup": self._run_backup,
            "E7_end_to_end": self._run_end_to_end,
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
            allowed_subtypes=["missing_index", "cpu", "overall_workload"],
            anomaly_categories=["slow_sql", "resource_bottleneck", "traffic_surge"],
            user_constraints={"baseline_sleep": 0.044, "baseline_threads": 80},
        )
        context = planner.gather_context(request)
        response = planner.plan(request, context)
        revised = planner.revise(request, "reduce the risk and keep only missing_index")
        revised_context = planner.gather_context(revised)
        revised_response = planner.plan(revised, revised_context)

        request_path = out_dir / "request.json"
        request_path.write_text(json.dumps(_to_jsonable({"missing_request": request_missing, "request": request, "revised_request": revised}), ensure_ascii=True, indent=2))
        plan_path = out_dir / "plan.json"
        plan_path.write_text(json.dumps(_to_jsonable({"missing_response": response_missing, "response": response, "revised_response": revised_response}), ensure_ascii=True, indent=2))

        passed = bool(response_missing.follow_up_questions) and response.plan is not None and response.planner_decision is not None and revised_response.plan is not None
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
                "allowed_subtypes": ["missing_index", "cpu", "overall_workload"],
                "openai_available": self.components.llm_client.available(),
            },
            "artifacts": {"request": str(request_path), "plan": str(plan_path)},
            "observations": observations,
            "failure_reason": None if passed else "planner did not produce the expected structured plans",
            "next_action": "Ready for live LLM-path execution." if self.components.llm_client.available() else "Provide LLM credentials before live execution.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_slow_sql(self, out_dir: Path, database: str) -> Dict[str, Any]:
        return self._run_agent_case(out_dir, database, "SlowSQLAgent", ["missing_index"], ["slow_sql"], "slow_sql")

    def _run_resource(self, out_dir: Path, database: str) -> Dict[str, Any]:
        return self._run_agent_case(out_dir, database, "ResourceBottleneckAgent", ["cpu"], ["resource_bottleneck"], "resource_bottleneck")

    def _run_traffic(self, out_dir: Path, database: str) -> Dict[str, Any]:
        return self._run_agent_case(out_dir, database, "TrafficSurgeAgent", ["overall_workload"], ["traffic_surge"], "traffic_surge")

    def _run_lock(self, out_dir: Path, database: str) -> Dict[str, Any]:
        return self._run_agent_case(out_dir, database.replace("_base", "_copy"), "LockConflictAgent", ["record_lock"], ["lock_conflict"], "lock_conflict")

    def _run_backup(self, out_dir: Path, database: str) -> Dict[str, Any]:
        return self._run_agent_case(out_dir, database.replace("_base", "_copy"), "BackupAgent", ["database_table_backup"], ["database_backup"], "database_backup")

    def _run_agent_case(self, out_dir: Path, database: str, agent_name: str, subtypes, categories, agent_type: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = ExperimentRequest(user_goal=f"Validate {agent_name}", target_database=database, allowed_subtypes=subtypes, anomaly_categories=categories)
        context = self.components.planner.gather_context(request)
        response = self.components.planner.plan(request, context)
        tasks = [task for task in (response.plan.tasks if response.plan else []) if task.agent_type == agent_type]
        task_path = out_dir / "task.json"
        task_path.write_text(json.dumps(_to_jsonable({"tasks": tasks}), ensure_ascii=True, indent=2))
        passed = bool(tasks)
        result = {
            "status": "pass" if passed else "fail",
            "agent": agent_name,
            "input_summary": {"database": database, "allowed_subtypes": subtypes},
            "artifacts": {"task": str(task_path)},
            "observations": [f"task_count={len(tasks)}"],
            "failure_reason": None if passed else f"{agent_name} did not emit any tasks",
            "next_action": "Task generation succeeded." if passed else "Inspect planner/task-agent routing.",
        }
        self._write_result_files(out_dir, result)
        return result

    def _run_end_to_end(self, out_dir: Path, database: str) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        planner = self.components.planner
        request = ExperimentRequest(
            user_goal="Plan a combined validation run for missing_index, cpu, overall_workload, and record_lock",
            target_database=database,
            allowed_subtypes=["missing_index", "cpu", "overall_workload", "record_lock"],
            anomaly_categories=["slow_sql", "resource_bottleneck", "traffic_surge", "lock_conflict"],
            user_constraints={"baseline_sleep": 0.044, "baseline_threads": 80},
        )
        context = planner.gather_context(request)
        response = planner.plan(request, context)
        plan_path = out_dir / "plan.json"
        plan_path.write_text(json.dumps(_to_jsonable({"plan": response}), ensure_ascii=True, indent=2))

        initial_agents = {task.agent_type for task in (response.plan.tasks if response.plan else [])}
        passed = {"slow_sql", "resource_bottleneck", "traffic_surge", "lock_conflict"}.issubset(initial_agents)
        result = {
            "status": "pass" if passed else "fail",
            "agent": "GlobalPlannerAgent+TaskAgents",
            "input_summary": {"database": database, "allowed_subtypes": request.allowed_subtypes},
            "artifacts": {"plan": str(plan_path)},
            "observations": [f"initial_agents={sorted(initial_agents)}"],
            "failure_reason": None if passed else "end-to-end planning did not fan out to the expected task agent mix",
            "next_action": "Ready for live suite execution." if passed else "Inspect planner fan-out.",
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
