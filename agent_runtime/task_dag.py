from __future__ import annotations

from typing import Iterable

from agent_runtime.types import PlannerDecision, TaskAgentOutput, TaskDAG, TaskDAGEdge, TaskDAGNode, TaskSpec


class TaskDAGBuilder:
    def build(self, tasks: Iterable[TaskSpec], planner_decision: PlannerDecision | None = None) -> TaskDAG:
        task_list = list(tasks)
        nodes = {
            task.task_id: TaskDAGNode(
                task_id=task.task_id,
                task_spec=task,
                start_after_sec=task.start_after_sec,
                start_condition=task.start_condition,
            )
            for task in task_list
        }
        edges: list[TaskDAGEdge] = []
        by_anomaly = {task.anomaly_type: task.task_id for task in task_list}
        for task in task_list:
            for dependency in task.dependencies:
                source = by_anomaly.get(dependency, dependency)
                if source in nodes:
                    edges.append(TaskDAGEdge(from_task=source, to_task=task.task_id, condition=task.start_condition.get("condition", "")))

        if not edges and planner_decision and planner_decision.global_plan:
            for source_anomaly, target_anomaly in planner_decision.global_plan.task_dependencies:
                source = by_anomaly.get(source_anomaly, source_anomaly)
                target = by_anomaly.get(target_anomaly, target_anomaly)
                if source in nodes and target in nodes:
                    edges.append(TaskDAGEdge(from_task=source, to_task=target, condition="causal_dependency"))
        if not edges and planner_decision and planner_decision.activation_order:
            ordered = [by_anomaly[item] for item in planner_decision.activation_order if item in by_anomaly]
            for source, target in zip(ordered, ordered[1:]):
                edges.append(TaskDAGEdge(from_task=source, to_task=target, condition="activation_order"))

        schedule = {
            task.task_id: {
                "start_after_sec": task.start_after_sec,
                "start_condition": task.start_condition,
                "task_role": task.task_role,
            }
            for task in task_list
        }
        return TaskDAG(tasks=nodes, edges=edges, schedule=schedule)

    @staticmethod
    def task_agent_outputs(tasks: Iterable[TaskSpec]) -> list[TaskAgentOutput]:
        outputs: list[TaskAgentOutput] = []
        for task in tasks:
            outputs.append(
                TaskAgentOutput(
                    agent_name=task.agent_type,
                    subgoal=task.anomaly_type,
                    local_hypothesis=task.explanation or f"{task.agent_type} will inject {task.anomaly_type}.",
                    task_spec=task,
                    expected_metrics=task.expected_metrics,
                    local_success_criteria=task.local_success_criteria,
                    risk_assessment=task.risk_assessment,
                    cleanup_actions=task.cleanup_actions or task.rollback_steps,
                    confidence=float(task.risk_assessment.get("confidence", 0.6)) if task.risk_assessment else 0.6,
                )
            )
        return outputs
