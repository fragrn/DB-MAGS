"""
Task DAG builder: assembles TaskSpec dicts into an ExecutableTaskDAG,
adds edges from explicit dependencies and activation order,
validates topological order (cycle detection).
"""

from __future__ import annotations

from collections import deque
from typing import Any, List, Optional

from agent.types import ExecutableTaskDAG, TaskDAGEdge, TaskSpec


def build_task_dag(
    task_specs: list[dict[str, Any]],
    dependencies: Optional[List[List[str]]] = None,
) -> dict[str, Any]:
    """
    Build an ExecutableTaskDAG dict from a list of TaskSpec dicts.

    Parameters
    ----------
    task_specs: list of TaskSpec-as-dict objects
    dependencies: list of [source_task_id, target_task_id] pairs defining edges

    Returns
    -------
    dict with keys: tasks, edges, schedule
    """
    tasks: dict[str, dict] = {}
    for spec in task_specs:
        task_id = spec.get("task_id")
        if not task_id:
            import uuid
            task_id = f"task_{uuid.uuid4().hex[:6]}"
            spec["task_id"] = task_id
        if task_id in tasks:
            raise ValueError(f"Duplicate task_id: {task_id}")
        tasks[task_id] = spec

    # Build edges from explicit dependencies
    edges: list[TaskDAGEdge] = []
    task_ids = set(tasks.keys())

    for dep in (dependencies or []):
        if len(dep) != 2:
            raise ValueError(f"Invalid dependency pair: {dep}")
        src, dst = dep[0], dep[1]
        if src not in task_ids:
            raise ValueError(f"Dependency source task '{src}' not in task set")
        if dst not in task_ids:
            raise ValueError(f"Dependency target task '{dst}' not in task set")
        edges.append(TaskDAGEdge(source=src, target=dst, condition=""))

    # Add edges from TaskSpec.dependencies field
    for task_id, spec in tasks.items():
        for dep_id in spec.get("dependencies", []):
            if dep_id not in task_ids:
                raise ValueError(f"TaskSpec.dependencies references unknown task '{dep_id}'")
            edges.append(TaskDAGEdge(source=dep_id, target=task_id, condition=""))

    # Validate cycle-free using Kahn's algorithm
    ordered = topological_order(tasks, edges)

    # Build schedule: start times based on topological order and start_after_sec
    schedule: dict[str, float] = {}
    offset = 0.0
    for task_id in ordered:
        offset += tasks[task_id].get("start_after_sec", 0.0)
        schedule[task_id] = offset

    return {
        "tasks": tasks,
        "edges": [e.__dict__ for e in edges],
        "schedule": schedule,
    }


def topological_order(
    tasks: dict[str, dict],
    edges: list[TaskDAGEdge],
) -> list[str]:
    """
    Return tasks in topological order (Kahn's algorithm).

    Raises ValueError if a cycle is detected.
    """
    # in-degree
    indegree: dict[str, int] = {tid: 0 for tid in tasks}
    for e in edges:
        indegree[e.target] = indegree.get(e.target, 0) + 1

    queue = deque([tid for tid, d in indegree.items() if d == 0])
    ordered: list[str] = []

    while queue:
        node = queue.popleft()
        ordered.append(node)
        for e in edges:
            if e.source == node:
                indegree[e.target] -= 1
                if indegree[e.target] == 0:
                    queue.append(e.target)

    if len(ordered) != len(tasks):
        raise ValueError("Cycle detected in task DAG")

    return ordered
