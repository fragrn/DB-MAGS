from __future__ import annotations

from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import TaskResult, TaskSpec


class TaskExecutor:
    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def execute(self, task_spec: TaskSpec) -> TaskResult:
        runner = self.skills.get("run_injection_skill")
        metrics = self.skills.get("collect_metrics_skill")
        cleanup = self.skills.get("cleanup_skill")
        try:
            chaos = self.skills.get("chaosblade_injection_skill")
        except KeyError:
            chaos = None
        artifacts = {"prechecks": task_spec.prechecks, "validation": task_spec.validation_steps}
        errors = []
        executed_ok = True
        dynamic_cleanup_results = []
        for step in task_spec.execution_steps:
            result = runner.execute(step=step)
            artifacts.setdefault("execution", []).append(result)
            if not result.get("executed"):
                executed_ok = False
                errors.append(result.get("error") or f"step failed: {step}")
            elif chaos is not None and result.get("uid"):
                cleanup_result = chaos.cleanup(result["uid"])
                dynamic_cleanup_results.append(cleanup_result)
                if not cleanup_result.get("cleaned"):
                    errors.append(cleanup_result.get("error") or "chaosblade cleanup failed")
        metric_payload = metrics.execute(task_id=task_spec.task_id, anomaly_type=task_spec.anomaly_type, artifacts={"executed": executed_ok})
        cleanup_payload = cleanup.execute(rollback_steps=task_spec.rollback_steps, runner=lambda step: runner.execute(step=step))
        if dynamic_cleanup_results:
            artifacts["dynamic_cleanup"] = dynamic_cleanup_results
        status = "completed" if executed_ok else "failed"
        if executed_ok and task_spec.validation_steps:
            all_validated = all(step.get("result", {}).get("validated", False) for step in task_spec.validation_steps)
            if not all_validated:
                status = "executed_but_not_validated"
        cleanup_success = cleanup_payload.get("cleaned")
        if dynamic_cleanup_results:
            cleanup_success = cleanup_success and all(item.get("cleaned") for item in dynamic_cleanup_results)
        return TaskResult(
            task_id=task_spec.task_id,
            status=status,
            artifacts=artifacts,
            observed_signals=metric_payload.get("signals", []),
            errors=errors,
            cleanup_status="completed" if cleanup_success else "skipped",
        )
