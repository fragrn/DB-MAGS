from typing import Dict, Iterable, List, Optional

from agent.metadata import MetadataInspector
from agent.models import DatabaseProfile, TaskSpec
from agent.task_agents import CpuContentionAgent, MissingIndexAgent


class GlobalAgent:
    def __init__(self, inspector: Optional[MetadataInspector] = None, task_agents: Optional[Iterable] = None):
        self.inspector = inspector
        agents = task_agents or (CpuContentionAgent(), MissingIndexAgent())
        self.task_agents = {agent.name: agent for agent in agents}

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
        return plan
