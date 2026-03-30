from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List

from agent_runtime.executor import TaskExecutor
from agent_runtime.types import TaskResult, TaskSpec


class TaskScheduler:
    def __init__(self, executor: TaskExecutor, max_workers: int = 3):
        self.executor = executor
        self.max_workers = max_workers

    def run(self, tasks: Iterable[TaskSpec]) -> List[TaskResult]:
        task_list = list(tasks)
        if not task_list:
            return []
        results: List[TaskResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {pool.submit(self.executor.execute, task): task for task in task_list}
            for future in as_completed(future_map):
                try:
                    results.append(future.result())
                except Exception as exc:
                    task = future_map[future]
                    results.append(
                        TaskResult(
                            task_id=task.task_id,
                            status="failed",
                            errors=[str(exc)],
                            cleanup_status="skipped",
                        )
                    )
        return sorted(results, key=lambda item: item.task_id)
