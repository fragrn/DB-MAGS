from typing import Dict, Iterable, List, Optional

from agent.metadata import MetadataInspector
from agent.models import DatabaseProfile, TaskSpec
from agent.task_agents import CpuContentionAgent, MissingIndexAgent


class GlobalAgent:
    def __init__(self, inspector: Optional[MetadataInspector] = None, task_agents: Optional[Iterable] = None, llm_client=None):
        self.inspector = inspector
        agents = task_agents or (CpuContentionAgent(), MissingIndexAgent())
        self.task_agents = {agent.name: agent for agent in agents}
        self.llm_client = llm_client
        self.last_plan_summary: Optional[str] = None
        self.last_llm_error: Optional[str] = None
        self.last_llm_endpoint: Optional[str] = None

    def collect_profile(self, schema_name: Optional[str] = None) -> DatabaseProfile:
        if self.inspector is None:
            self.inspector = MetadataInspector()
        return self.inspector.inspect(schema_name=schema_name)

    def plan(self, profile: DatabaseProfile, runtime_context: Dict) -> List[TaskSpec]:
        enabled_agents = runtime_context.get("enabled_agents") or list(self.task_agents.keys())
        plan: List[TaskSpec] = []
        for agent_name in enabled_agents:
            agent = self.task_agents.get(agent_name)
            if not agent:
                continue
            plan.extend(agent.plan(profile, runtime_context))
        self._summarize_plan(profile, plan)
        return plan

    def runtime_metadata(self) -> Dict[str, Optional[str]]:
        model = None
        available = False
        if self.llm_client is not None:
            available = self.llm_client.available()
            model = self.llm_client.config.openai_model
        return {
            "openai_available": available,
            "openai_model": model,
            "openai_connected": bool(self.last_plan_summary) and not self.last_llm_error,
            "openai_error": self.last_llm_error,
            "openai_endpoint": self.last_llm_endpoint,
            "planner_summary": self.last_plan_summary,
        }

    def _summarize_plan(self, profile: DatabaseProfile, plan: List[TaskSpec]) -> None:
        self.last_plan_summary = None
        self.last_llm_error = None
        self.last_llm_endpoint = None
        if self.llm_client is None:
            return
        result = self.llm_client.summarize_plan(
            profile_summary={
                "schema_name": profile.schema_name,
                "table_count": len(profile.tables),
                "tables": [table.name for table in profile.tables[:12]],
            },
            plan_summary={
                "task_count": len(plan),
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "agent_name": task.agent_name,
                        "description": task.metadata.get("description"),
                    }
                    for task in plan
                ],
            },
        )
        self.last_plan_summary = result.text or None
        self.last_llm_error = result.error
        self.last_llm_endpoint = result.endpoint
