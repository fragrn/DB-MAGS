from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional

from agent_runtime.planner import GlobalPlannerAgent
from agent_runtime.scheduler import TaskScheduler
from agent_runtime.types import ExperimentRequest, ExperimentResult, MessageEvent
from agent_runtime.utils import to_pretty_json


class CLIConversationOrchestrator:
    def __init__(self, planner: GlobalPlannerAgent, scheduler: TaskScheduler):
        self.planner = planner
        self.scheduler = scheduler
        self.history: List[MessageEvent] = []
        self.latest_plan = None

    def run(self, request: ExperimentRequest) -> ExperimentResult:
        context = self.planner.gather_context(request)
        while True:
            response = self.planner.plan(request, context)
            if response.follow_up_questions:
                for question in response.follow_up_questions:
                    self._record("assistant", question)
                    answer = input(f"agent> {question}\nuser> ").strip()
                    self._record("user", answer)
                    self._apply_answer(request, answer)
                context = self.planner.gather_context(request)
                continue
            self.latest_plan = response.plan
            self._record("assistant", self.render_plan())
            while True:
                command = input("agent> Enter one of [show plan|revise <text>|confirm|cancel]\nuser> ").strip()
                self._record("user", command)
                if command == "show plan":
                    print(self.render_plan())
                    continue
                if command.startswith("revise "):
                    request = self.planner.revise(request, command[len("revise "):].strip())
                    context = self.planner.gather_context(request)
                    break
                if command == "cancel":
                    return ExperimentResult(plan=self.latest_plan, status="cancelled", summary="User cancelled execution.")
                if command == "confirm":
                    task_results = self.scheduler.run(self.latest_plan.tasks)
                    status = "completed"
                    if any(result.status == "failed" for result in task_results):
                        status = "partial_failure"
                    return ExperimentResult(
                        plan=self.latest_plan,
                        task_results=task_results,
                        status=status,
                        summary=self._summarize_results(task_results),
                    )
                print("Unsupported command. Use show plan, revise <text>, confirm, or cancel.")

    def render_plan(self) -> str:
        if not self.latest_plan:
            return "No plan available."
        return to_pretty_json(self.latest_plan)

    def _apply_answer(self, request: ExperimentRequest, answer: str) -> None:
        if not request.target_database:
            request.target_database = answer or request.target_database
            return
        if not request.allowed_anomalies:
            request.allowed_anomalies = [item.strip() for item in answer.split(",") if item.strip()]
            return
        request.user_constraints.setdefault("extra_answers", []).append(answer)

    def _record(self, role: str, content: str) -> None:
        self.history.append(MessageEvent(role=role, content=content))

    @staticmethod
    def _summarize_results(task_results) -> str:
        if not task_results:
            return "No tasks were executed."
        return "; ".join(f"{result.task_id}:{result.status}" for result in task_results)
