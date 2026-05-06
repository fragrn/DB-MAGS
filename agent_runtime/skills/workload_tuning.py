from __future__ import annotations

from typing import Dict

from agent_runtime.skills.base import Skill


class WorkloadTuningSkill(Skill):
    name = "workload_tuning_skill"

    def execute(self, mode: str, baseline_sleep: float, baseline_threads: int) -> Dict[str, object]:
        if mode == "overall_workload":
            return {
                "mode": mode,
                "sleep_time": min(baseline_sleep, 0.005),
                "thread_count": max(baseline_threads, 500),
                "description": "Increase overall workload by reducing sleep time and raising concurrency.",
            }
        if mode == "single_sql":
            return {
                "mode": mode,
                "sleep_time": max(baseline_sleep / 2, 0.001),
                "thread_count": max(min(baseline_threads, 24), 4),
                "repeat": 25,
                "description": "Increase pressure on a single SQL statement by repeating it with a short pause.",
            }
        return {
            "mode": mode,
            "sleep_time": max(baseline_sleep / 2, 0.001),
            "thread_count": baseline_threads,
            "description": "Increase frequency of a targeted SQL task.",
        }
