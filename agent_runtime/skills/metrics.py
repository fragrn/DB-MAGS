from __future__ import annotations

from typing import Dict, List

from agent_runtime.skills.base import Skill


class CollectMetricsSkill(Skill):
    name = "collect_metrics_skill"

    def execute(self, task_id: str, anomaly_type: str, artifacts: Dict[str, object]) -> Dict[str, object]:
        signals: List[str] = [f"task={task_id}", f"anomaly={anomaly_type}"]
        if artifacts.get("validated"):
            signals.append("validated=true")
        if artifacts.get("executed"):
            signals.append("executed=true")
        return {"signals": signals}


class CleanupSkill(Skill):
    name = "cleanup_skill"

    def execute(self, rollback_steps: List[Dict[str, object]], runner) -> Dict[str, object]:
        results = []
        success = True
        for step in rollback_steps:
            result = runner(step)
            results.append(result)
            if not result.get("executed", result.get("cleaned", False)):
                success = False
        return {"cleaned": success, "results": results}
