from __future__ import annotations

from typing import List

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import DBContextSummary, ExperimentRequest, TaskSpec
from agent_runtime.utils import slugify


SQL_ANOMALIES = {
    "lock": "record_lock",
    "record_lock": "record_lock",
    "table_lock": "table_lock",
    "metadata_lock": "metadata_lock",
    "slow": "missing_index",
    "missing_index": "missing_index",
    "implicit_conversion": "implicit_conversion",
    "join": "multi_table_join",
    "multi_table_join": "multi_table_join",
    "order_by": "order_by",
    "group_by": "group_by",
    "scan": "large_table_scan",
    "large_table_scan": "large_table_scan",
}


class SQLAnomalyAgent(BaseTaskAgent):
    agent_type = "sql"

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def prepare(self, context: DBContextSummary, request: ExperimentRequest):
        requested = [item for item in request.allowed_anomalies if item in SQL_ANOMALIES]
        if not requested:
            return []
        anomaly_type = SQL_ANOMALIES[requested[0]]
        generator = self.skills.get("generate_sql_candidate_skill")
        validator = self.skills.get("validate_sql_skill")
        explainer = self.skills.get("explain_sql_skill")
        allowed_tables = [table.name for table in context.tables]
        task_specs: List[TaskSpec] = []
        for index, candidate in enumerate(generator.execute(anomaly_type=anomaly_type, db_context=context)[:3]):
            validation = validator.execute(sql=candidate, allowed_tables=allowed_tables, anomaly_type=anomaly_type)
            if not validation["valid"]:
                continue
            explain = explainer.execute(validation["sql"])
            if not explain.get("validated") and context.tables:
                continue
            task_id = f"sql-{slugify(anomaly_type)}-{index + 1}"
            task_specs.append(
                TaskSpec(
                    task_id=task_id,
                    agent_type=self.agent_type,
                    anomaly_type=anomaly_type,
                    title=f"SQL anomaly: {anomaly_type}",
                    inputs={"sql": validation["sql"]},
                    prechecks=[{"name": "validate_sql", "result": validation}],
                    execution_steps=[{"kind": "sql", "sql": validation["sql"]}],
                    validation_steps=[{"kind": "explain", "sql": validation["sql"], "result": explain}],
                    rollback_steps=[],
                    explanation=f"Execute candidate SQL for {anomaly_type} after validation and EXPLAIN verification.",
                )
            )
        return task_specs[:1]

    def explain(self, task_spec: TaskSpec) -> str:
        return task_spec.explanation
