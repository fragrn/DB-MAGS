from __future__ import annotations

from agent_runtime.agents import ResourceAgent, SQLAnomalyAgent, TrafficAgent
from agent_runtime.config import RuntimeConfig
from agent_runtime.conversation import CLIConversationOrchestrator
from agent_runtime.executor import TaskExecutor
from agent_runtime.planner import GlobalPlannerAgent
from agent_runtime.scheduler import TaskScheduler
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.skills.chaosblade import ChaosBladeInjectionSkill
from agent_runtime.skills.distribution_inspection import InspectDistributionSkill
from agent_runtime.skills.injection_bridge import RunInjectionSkill
from agent_runtime.skills.metrics import CleanupSkill, CollectMetricsSkill
from agent_runtime.skills.schema_inspection import InspectSchemaSkill
from agent_runtime.skills.sql_explain import ExplainSQLSkill
from agent_runtime.skills.sql_generation import GenerateSQLCandidateSkill
from agent_runtime.skills.sql_validation import ValidateSQLSkill
from agent_runtime.skills.workload_tuning import WorkloadTuningSkill
from agent_runtime.llm import ResponsesAPIClient


def build_runtime(config: RuntimeConfig | None = None) -> CLIConversationOrchestrator:
    config = config or RuntimeConfig.from_env()
    llm_client = ResponsesAPIClient(config)
    skills = SkillRegistry(
        [
            InspectSchemaSkill(),
            InspectDistributionSkill(),
            GenerateSQLCandidateSkill(llm_client=llm_client, temperature=config.sql_temperature),
            ValidateSQLSkill(),
            ExplainSQLSkill(),
            ChaosBladeInjectionSkill(),
            WorkloadTuningSkill(),
            RunInjectionSkill(),
            CollectMetricsSkill(),
            CleanupSkill(),
        ]
    )
    agents = [SQLAnomalyAgent(skills), ResourceAgent(skills), TrafficAgent(skills)]
    planner = GlobalPlannerAgent(config=config, skills=skills, task_agents=agents)
    executor = TaskExecutor(skills)
    scheduler = TaskScheduler(executor=executor, max_workers=config.max_concurrency)
    return CLIConversationOrchestrator(planner=planner, scheduler=scheduler)
