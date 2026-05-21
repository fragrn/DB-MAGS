from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


class ReportGenerator:
    def build(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluation = payload.get("evaluation")
        reward = getattr(evaluation, "reward", {}) if evaluation is not None else {}
        success = getattr(evaluation, "success", False) if evaluation is not None else False
        return {
            "experiment_goal": payload.get("request", {}).get("user_goal", ""),
            "target_anomaly": payload.get("request", {}).get("target_anomaly", ""),
            "target_mode": payload.get("request", {}).get("target_mode", ""),
            "environment": _to_jsonable(payload.get("environment")),
            "global_plan": _to_jsonable(payload.get("global_plan")),
            "task_dag": _to_jsonable(payload.get("task_dag")),
            "execution_trace": _to_jsonable(payload.get("execution_trace")),
            "evaluation": _to_jsonable(evaluation),
            "reflection": _to_jsonable(payload.get("reflection")),
            "cleanup": _to_jsonable(payload.get("cleanup")),
            "summary": {
                "success": success,
                "reward": reward,
                "reason": getattr(evaluation, "reason", "") if evaluation is not None else "",
            },
        }


def _to_jsonable(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(val) for key, val in value.items()}
    return value
