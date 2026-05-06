from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.agents import (
    BackupAgent,
    LockConflictAgent,
    ResourceBottleneckAgent,
    SlowSQLAgent,
    TrafficSurgeAgent,
)
from agent_runtime.config import RuntimeConfig
from agent_runtime.conversation import CLIConversationOrchestrator
from agent_runtime.executor import TaskExecutor
from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.planner import GlobalPlannerAgent
from agent_runtime.scheduler import TaskScheduler
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.skills.build_planner_context import BuildPlannerContextSkill
from agent_runtime.skills.chaosblade import ChaosBladeInjectionSkill
from agent_runtime.skills.distribution_inspection import InspectDistributionSkill
from agent_runtime.skills.injection_bridge import RunInjectionSkill
from agent_runtime.skills.metrics import CleanupSkill, CollectMetricsSkill
from agent_runtime.skills.prepare_backup_task import PrepareBackupTaskSkill
from agent_runtime.skills.prepare_excessive_index import PrepareExcessiveIndexSkill
from agent_runtime.skills.prepare_implicit_conversion_support import PrepareImplicitConversionSupportSkill
from agent_runtime.skills.prepare_lock_sql import PrepareLockSQLSkill
from agent_runtime.skills.prepare_sortscan_support import PrepareSortScanSupportSkill
from agent_runtime.skills.schema_inspection import InspectSchemaSkill
from agent_runtime.skills.sql_explain import ExplainSQLSkill
from agent_runtime.skills.sql_generation import GenerateSQLCandidateSkill
from agent_runtime.skills.sql_validation import ValidateSQLSkill
from agent_runtime.skills.workload_tuning import WorkloadTuningSkill


@dataclass
class RuntimeComponents:
    config: RuntimeConfig
    llm_client: ResponsesAPIClient
    skills: SkillRegistry
    planner: GlobalPlannerAgent
    executor: TaskExecutor
    scheduler: TaskScheduler


def build_components(config: RuntimeConfig | None = None) -> RuntimeComponents:
    config = config or RuntimeConfig.from_env()
    llm_client = ResponsesAPIClient(config)
    skills = SkillRegistry(
        [
            InspectSchemaSkill(),
            InspectDistributionSkill(),
            BuildPlannerContextSkill(),
            GenerateSQLCandidateSkill(llm_client=llm_client, temperature=config.sql_temperature),
            ValidateSQLSkill(),
            ExplainSQLSkill(),
            ChaosBladeInjectionSkill(),
            WorkloadTuningSkill(),
            PrepareLockSQLSkill(),
            PrepareBackupTaskSkill(),
            PrepareExcessiveIndexSkill(),
            PrepareSortScanSupportSkill(),
            PrepareImplicitConversionSupportSkill(),
            RunInjectionSkill(),
            CollectMetricsSkill(),
            CleanupSkill(),
        ]
    )
    agents = [
        LockConflictAgent(skills),
        TrafficSurgeAgent(skills),
        SlowSQLAgent(skills),
        ResourceBottleneckAgent(skills),
        BackupAgent(skills),
    ]
    planner = GlobalPlannerAgent(config=config, skills=skills, task_agents=agents)
    executor = TaskExecutor(skills)
    scheduler = TaskScheduler(executor=executor, max_workers=config.max_concurrency)
    return RuntimeComponents(
        config=config,
        llm_client=llm_client,
        skills=skills,
        planner=planner,
        executor=executor,
        scheduler=scheduler,
    )


def build_runtime(config: RuntimeConfig | None = None) -> CLIConversationOrchestrator:
    components = build_components(config)
    return CLIConversationOrchestrator(planner=components.planner, scheduler=components.scheduler)
