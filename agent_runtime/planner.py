from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Dict, List, Sequence

from agent_runtime.agents.base import BaseTaskAgent
from agent_runtime.config import RuntimeConfig
from agent_runtime.llm import ResponsesAPIClient
from agent_runtime.skill_registry import SkillRegistry
from agent_runtime.types import (
    DBContextSummary,
    ExperimentPlan,
    ExperimentRequest,
    GlobalPlan,
    PlannedAnomaly,
    PlanningMemoryContext,
    PlannerDecision,
    PlannerResponse,
    TaskAgentInput,
)


CATEGORY_TO_SUBTYPES = {
    "lock_conflict": ["record_lock", "table_lock", "metadata_lock"],
    "traffic_surge": ["single_sql", "overall_workload"],
    "slow_sql": [
        "missing_index",
        "excessive_index",
        "implicit_conversion",
        "multi_table_join",
        "order_by",
        "group_by",
        "large_table_scan",
    ],
    "resource_bottleneck": ["cpu", "io", "network", "memory", "disk"],
    "database_backup": ["database_table_backup"],
}

SUBTYPE_TO_AGENT = {
    "record_lock": "lock_conflict",
    "table_lock": "lock_conflict",
    "metadata_lock": "lock_conflict",
    "single_sql": "traffic_surge",
    "overall_workload": "traffic_surge",
    "missing_index": "slow_sql",
    "excessive_index": "slow_sql",
    "implicit_conversion": "slow_sql",
    "multi_table_join": "slow_sql",
    "order_by": "slow_sql",
    "group_by": "slow_sql",
    "large_table_scan": "slow_sql",
    "cpu": "resource_bottleneck",
    "io": "resource_bottleneck",
    "network": "resource_bottleneck",
    "memory": "resource_bottleneck",
    "disk": "resource_bottleneck",
    "database_table_backup": "database_backup",
}

LOCKISH_SUBTYPES = {"record_lock", "table_lock", "metadata_lock", "database_table_backup"}
AUTO_MULTI_PRIORITY = ["missing_index", "cpu", "record_lock", "overall_workload", "database_table_backup"]
# Causal-chain evaluation nodes -> concrete injectable subtypes for task agents.
CHAIN_NODE_TO_SUBTYPE = {
    "traffic_surge": "overall_workload",
    "connections_up": "overall_workload",
    "lock_contention": "record_lock",
    "slow_query": "missing_index",
    "qps_drop": "missing_index",
}

logger = logging.getLogger(__name__)


class GlobalPlannerAgent:
    def __init__(self, config: RuntimeConfig, skills: SkillRegistry, task_agents: Sequence[BaseTaskAgent]):
        self.config = config
        self.skills = skills
        self.task_agents = list(task_agents)
        self.task_agent_map = {agent.agent_type: agent for agent in self.task_agents}
        self.llm_client = ResponsesAPIClient(config)
        self.last_llm_result = None

    def gather_context(self, request: ExperimentRequest) -> DBContextSummary:
        database = request.target_database or self.config.default_database
        context = self.skills.get("inspect_schema_skill").execute(database=database)
        context.distribution = self.skills.get("inspect_distribution_skill").execute(database=database)
        return context

    def plan(self, request: ExperimentRequest, context: DBContextSummary) -> PlannerResponse:
        missing = self._missing_information(request, context)
        if missing:
            return PlannerResponse(follow_up_questions=missing, reasoning="Need more information before planning.")
        planner_context = self.skills.get("build_planner_context_skill").execute(
            request_goal=request.user_goal,
            allowed_categories=self._normalize_categories(request),
            allowed_subtypes=self._normalize_subtypes(request),
            context=context,
            database_topology=request.database_topology,
        )
        planner_decision = self._planner_decision(request, planner_context)
        planner_decision.global_plan = self._global_plan_from_decision(request, planner_decision)
        task_agent_inputs: list[TaskAgentInput] = []
        task_agent_outputs = []
        for agent_type, agent in self.task_agent_map.items():
            planned_tasks = [item for item in planner_decision.planned_tasks if item.source_agent == agent_type]
            inputs = {
                item.anomaly_subtype: self._task_agent_input(request, context, planner_context, planner_decision, item)
                for item in planned_tasks
            }
            task_agent_inputs.extend(inputs.values())
            task_agent_outputs.extend(agent.prepare(context, request, planner_tasks=planned_tasks, task_inputs=inputs))
        tasks = [output.task_spec for output in task_agent_outputs]
        summary = planner_decision.llm_summary or self._fallback_summary(request, tasks)
        plan = ExperimentPlan(
            summary=summary,
            db_context_summary=self._describe_context(context),
            planner_decision=planner_decision,
            tasks=tasks,
            task_agent_inputs=task_agent_inputs,
            task_agent_outputs=task_agent_outputs,
            expected_signals=planner_decision.expected_signals or self._expected_signals(request),
            safety_checks=[
                "No task executes before explicit user confirmation when confirmation is enabled.",
                "SQL tasks pass static validation and EXPLAIN before execution.",
                "Task failures are isolated and cleanup is attempted per task.",
            ],
            cleanup_plan=planner_decision.cleanup_strategy or [
                "Run per-task rollback steps.",
                "Collect execution artifacts and validation evidence.",
            ],
        )
        return PlannerResponse(
            plan=plan,
            planner_decision=planner_decision,
            reasoning="Planner produced a structured anomaly plan.",
        )

    def revise(self, request: ExperimentRequest, revision_text: str) -> ExperimentRequest:
        request.user_constraints.setdefault("revisions", []).append(revision_text)
        request.user_goal = f"{request.user_goal}\nRevision: {revision_text}".strip()
        lowered = revision_text.lower()
        if "low" in lowered or "safer" in lowered or "reduce" in lowered:
            request.risk_level = "low"
        mentioned_subtypes = [subtype for subtype in SUBTYPE_TO_AGENT if subtype in lowered]
        mentioned_categories = [category for category in CATEGORY_TO_SUBTYPES if category in lowered]
        if "keep only" in lowered or "only " in lowered:
            request.allowed_subtypes = mentioned_subtypes
            request.anomaly_categories = mentioned_categories
            request.allowed_anomalies = list(mentioned_subtypes or mentioned_categories)
        elif "remove" in lowered or "drop" in lowered:
            request.allowed_subtypes = [item for item in request.allowed_subtypes if item not in mentioned_subtypes]
            request.anomaly_categories = [item for item in request.anomaly_categories if item not in mentioned_categories]
            request.allowed_anomalies = [item for item in request.allowed_anomalies if item not in mentioned_subtypes + mentioned_categories]
        elif "add" in lowered or "include" in lowered:
            for item in mentioned_subtypes:
                if item not in request.allowed_subtypes:
                    request.allowed_subtypes.append(item)
            for item in mentioned_categories:
                if item not in request.anomaly_categories:
                    request.anomaly_categories.append(item)
        return request

    def replan_from_reflection(self, request: ExperimentRequest, reflection, evaluation, memory_items=None) -> ExperimentRequest:
        task_parameter_updates = getattr(reflection, "task_parameter_updates", {}) or {}
        request.user_constraints.setdefault("reflections", []).append(
            {
                "failure_reason": getattr(reflection, "failure_reason", []),
                "suggested_changes": getattr(reflection, "suggested_changes", []),
                "task_parameter_updates": task_parameter_updates,
                "agent_specific_feedback": getattr(reflection, "agent_specific_feedback", {}),
                "evaluation_reason": getattr(evaluation, "reason", ""),
                "evaluation_reward": getattr(evaluation, "reward", {}),
                "memory_items": memory_items or [],
            }
        )
        overrides = request.user_constraints.setdefault("task_parameter_overrides", {})
        for subtype, updates in task_parameter_updates.items():
            if isinstance(updates, dict):
                overrides.setdefault(subtype, {}).update(updates)
        request.execution_window_seconds = min(max(request.execution_window_seconds + 4, 1), 60)
        if self._effective_mode(request) == "multi_auto" and not request.allowed_subtypes:
            request.allowed_subtypes = list(AUTO_MULTI_PRIORITY)
            request.allowed_anomalies = list(AUTO_MULTI_PRIORITY)
        return request

    @staticmethod
    def _missing_information(request: ExperimentRequest, context: DBContextSummary) -> List[str]:
        missing = []
        if not request.target_database:
            missing.append("Which database should I target? If you leave it blank, I can use the default TPCC database.")
        if request.mode == "single" and request.allowed_subtypes and len(request.allowed_subtypes) != 1 and request.user_constraints.get("enforce_single", False):
            missing.append("Single mode requires exactly one anomaly subtype.")
        if request.mode != "multi_auto" and not (request.allowed_anomalies or request.allowed_subtypes or request.anomaly_categories):
            missing.append(
                "Which anomaly categories or subtypes should I plan for? For example: slow_sql, lock_conflict, cpu, missing_index."
            )
        if not context.tables:
            missing.append("I could not read schema metadata. Please confirm the target database or provide table hints.")
        return missing

    @staticmethod
    def _effective_mode(request: ExperimentRequest) -> str:
        if request.mode == "single" and len(request.allowed_subtypes) > 1:
            return "multi_manual"
        return request.mode

    def _planner_decision(self, request: ExperimentRequest, planner_context: Dict[str, object]) -> PlannerDecision:
        if self._effective_mode(request) == "multi_auto":
            decision = self._auto_multi_planner_decision(request, planner_context)
            if decision is not None:
                return decision
        if self.llm_client.available():
            decision = self._llm_planner_decision(request, planner_context)
            if decision is not None:
                return decision
            return self._annotate_fallback(
                self._fallback_planner_decision(request),
                self.last_llm_result,
                "Planner LLM path fell back",
            )
        return self._fallback_planner_decision(request)

    def _llm_planner_decision(self, request: ExperimentRequest, planner_context: Dict[str, object]) -> PlannerDecision | None:
        system_prompt = (
            "You are a MySQL anomaly planner. Return JSON only. Choose anomaly subtypes from the allow-list, "
            "assign each subtype to the correct task agent, pick the execution database, and provide concise parameters."
        )
        user_prompt = json.dumps(
            {
                "request": {
                    "goal": request.user_goal,
                    "mode": self._effective_mode(request),
                    "risk_level": request.risk_level,
                    "execution_window_seconds": request.execution_window_seconds,
                    "allowed_categories": self._normalize_categories(request),
                    "allowed_subtypes": self._normalize_subtypes(request),
                    "database_topology": request.database_topology,
                    "user_constraints": request.user_constraints,
                    "planning_memory": request.user_constraints.get("planning_memory", {}),
                },
                "agent_catalog": CATEGORY_TO_SUBTYPES,
                "task_agent_map": SUBTYPE_TO_AGENT,
                "planner_context": planner_context,
                "reflection_memory_rules": {
                    "task_parameters_must_include_reflection_updates": True,
                    "backup_supported_parameters": ["source_table", "backup_table", "concurrent_with_probe", "background_duration_seconds"],
                    "slow_sql_supported_parameters": ["target_table", "sql", "background_threads", "background_sleep"],
                    "lock_supported_parameters": ["target_table", "predicate", "hold_seconds", "background_duration_seconds"],
                    "traffic_supported_parameters": ["thread_count", "sleep_time", "duration_seconds"],
                    "resource_supported_parameters": ["resource_type", "duration_seconds", "intensity"],
                },
                "rules": {
                    "lock_and_backup_use_copy_db": False,
                    "single_sql_and_overall_workload_use_base_db": True,
                    "multi_auto_min_items": 2,
                    "return_keys": [
                        "summary",
                        "selected_anomalies",
                        "task_assignments",
                        "database_mapping",
                        "task_parameters",
                        "expected_signals",
                        "cleanup_strategy",
                        "selection_rationale",
                    ],
                },
            },
            ensure_ascii=True,
        )
        result = self.llm_client.generate_json(system_prompt, user_prompt, self.config.planner_temperature)
        self.last_llm_result = result
        if result.used_fallback or not result.text:
            if result.error_message:
                logger.warning("Planner LLM path fell back: %s", result.error_message)
            return None
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            logger.warning("Planner LLM path returned non-JSON text; falling back.")
            return None
        selected = self._parse_selected_anomalies(payload.get("selected_anomalies", []))
        mode = self._effective_mode(request)
        if mode == "single":
            selected = selected[:1]
        if mode == "multi_auto" and len(selected) < 2:
            return None
        if not selected:
            return None
        raw_assignments = self._normalize_task_assignments(payload.get("task_assignments", {}), selected)
        assignments = {item: raw_assignments.get(item, SUBTYPE_TO_AGENT[item]) for item in selected}
        assignments = {
            item: assignments[item] if assignments[item] in self.task_agent_map else SUBTYPE_TO_AGENT[item]
            for item in selected
        }
        execution_database = str(payload.get("execution_database", request.target_database or self.config.default_database))
        raw_database_mapping = self._normalize_database_mapping(payload.get("database_mapping", {}), selected)
        database_mapping = {item: raw_database_mapping.get(item, execution_database) for item in selected}
        task_parameters = self._apply_parameter_overrides(self._normalize_task_parameters(payload.get("task_parameters", {}), selected), request, selected)
        expected_signals = self._normalize_string_list(payload.get("expected_signals", []))
        cleanup_strategy = self._normalize_string_list(payload.get("cleanup_strategy", []))
        selection_rationale = self._normalize_text_field(payload.get("selection_rationale", ""))
        activation_order = self._activation_order(selected)
        cleanup_order = list(reversed(activation_order))
        planned_tasks = [
            PlannedAnomaly(
                anomaly_subtype=item,
                category=self._category_for_subtype(item),
                source_agent=assignments[item],
                database=database_mapping[item],
                parameters=task_parameters.get(item, {}),
                rationale=selection_rationale or self._normalize_text_field(payload.get("rationale", f"LLM selected {item} for goal: {request.user_goal}")),
                expected_signals=expected_signals,
                cleanup_strategy=cleanup_strategy,
            )
            for item in selected
        ]
        return PlannerDecision(
            selected_anomalies=selected,
            task_assignments=assignments,
            database_mapping=database_mapping,
            task_parameters={item: task_parameters.get(item, {}) for item in selected},
            expected_signals=expected_signals,
            cleanup_strategy=cleanup_strategy,
            planned_tasks=planned_tasks,
            llm_summary=str(payload.get("summary", "")).strip(),
            selection_mode=mode,
            execution_database=execution_database,
            activation_order=activation_order,
            cleanup_order=cleanup_order,
            composite_experiment_name=self._composite_name(selected),
            selection_rationale=selection_rationale or str(payload.get("summary", "")).strip(),
            llm_used=True,
            llm_transport=getattr(result, "transport_used", ""),
        )

    def _auto_multi_planner_decision(self, request: ExperimentRequest, planner_context: Dict[str, object]) -> PlannerDecision | None:
        if self.llm_client.available():
            system_prompt = (
                "You design complex multi-anomaly MySQL experiments. Return JSON only. "
                "Pick a diverse set of at least 2 anomaly subtypes from the allow-list. "
                "selected_anomalies must be a JSON array of subtype strings (e.g. missing_index, record_lock), "
                "not objects."
            )
            user_prompt = json.dumps(
                {
                    "goal": request.user_goal,
                    "mode": self._effective_mode(request),
                    "allowed_categories": self._normalize_categories(request) or list(CATEGORY_TO_SUBTYPES),
                    "allowed_subtypes": self._normalize_subtypes(request) or sorted(SUBTYPE_TO_AGENT),
                    "planner_context": planner_context,
                    "planning_memory": request.user_constraints.get("planning_memory", {}),
                    "task_agent_map": SUBTYPE_TO_AGENT,
                    "return_keys": ["selected_anomalies", "task_parameters", "summary", "selection_rationale"],
                },
                ensure_ascii=True,
            )
            result = self.llm_client.generate_json(system_prompt, user_prompt, self.config.planner_temperature)
            self.last_llm_result = result
            if not result.used_fallback and result.text:
                try:
                    payload = json.loads(result.text)
                    chosen = self._parse_selected_anomalies(payload.get("selected_anomalies", []))
                    if len(chosen) >= 2:
                        request.allowed_subtypes = chosen
                        request.allowed_anomalies = list(chosen)
                        decision = self._fallback_planner_decision(request)
                        decision.selection_mode = "multi_auto"
                        decision.llm_summary = str(payload.get("summary", "")).strip()
                        decision.selection_rationale = self._normalize_text_field(payload.get("selection_rationale", decision.llm_summary))
                        decision.llm_used = True
                        decision.llm_transport = getattr(result, "transport_used", "")
                        planner_params = self._apply_parameter_overrides(self._normalize_task_parameters(payload.get("task_parameters", {}), chosen), request, chosen)
                        for task in decision.planned_tasks:
                            task.parameters.update(planner_params.get(task.anomaly_subtype, {}))
                            decision.task_parameters[task.anomaly_subtype] = task.parameters
                        return decision
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("Auto-multi LLM path returned invalid JSON structure; falling back.")
            elif result.error_message:
                logger.warning("Auto-multi LLM path fell back: %s", result.error_message)
        chosen: List[str] = []
        allowed = self._normalize_subtypes(request) or sorted(SUBTYPE_TO_AGENT)
        for subtype in AUTO_MULTI_PRIORITY:
            if subtype in allowed and subtype not in chosen:
                chosen.append(subtype)
        if len(chosen) < 2:
            for subtype in allowed:
                if subtype not in chosen:
                    chosen.append(subtype)
                if len(chosen) >= 3:
                    break
        if len(chosen) < 2:
            return None
        request.allowed_subtypes = chosen
        request.allowed_anomalies = list(chosen)
        decision = self._fallback_planner_decision(request)
        decision.selection_mode = "multi_auto"
        decision.selection_rationale = "Fallback auto-multi selected a diverse complex anomaly mix."
        return self._annotate_fallback(decision, self.last_llm_result, "Auto-multi LLM path fell back")

    def _fallback_planner_decision(self, request: ExperimentRequest) -> PlannerDecision:
        selected = self._normalize_subtypes(request)
        if not selected:
            for category in self._normalize_categories(request):
                selected.extend(CATEGORY_TO_SUBTYPES.get(category, []))
        selected = [item for item in selected if item in SUBTYPE_TO_AGENT]
        mode = self._effective_mode(request)
        if mode == "single":
            selected = selected[:1]
        if mode == "multi_auto" and len(selected) < 2:
            extras = [item for item in AUTO_MULTI_PRIORITY if item not in selected and item in SUBTYPE_TO_AGENT]
            selected.extend(extras[: max(0, 2 - len(selected))])
        if not selected:
            selected = ["missing_index"]
        execution_database = request.target_database or self.config.default_database
        assignments = {item: SUBTYPE_TO_AGENT[item] for item in selected}
        database_mapping = {item: execution_database for item in selected}
        task_parameters = self._apply_parameter_overrides({item: self._default_parameters(item, request) for item in selected}, request, selected)
        activation_order = self._activation_order(selected)
        planned_tasks = [
            PlannedAnomaly(
                anomaly_subtype=item,
                category=self._category_for_subtype(item),
                source_agent=assignments[item],
                database=database_mapping[item],
                parameters=task_parameters[item],
                rationale=f"Fallback planner selected {item} from request constraints.",
                expected_signals=self._signals_for_subtype(item),
                cleanup_strategy=["run rollback steps", "collect metrics"],
            )
            for item in selected
        ]
        return PlannerDecision(
            selected_anomalies=selected,
            task_assignments=assignments,
            database_mapping=database_mapping,
            task_parameters=task_parameters,
            expected_signals=sorted({signal for item in selected for signal in self._signals_for_subtype(item)}),
            cleanup_strategy=["run rollback steps", "collect metrics"],
            planned_tasks=planned_tasks,
            llm_summary="",
            selection_mode=request.mode,
            execution_database=execution_database,
            activation_order=activation_order,
            cleanup_order=list(reversed(activation_order)),
            composite_experiment_name=self._composite_name(selected),
            selection_rationale=(
                "Fallback auto-multi selected a complex anomaly mix." if mode == "multi_auto" else "Fallback planner selected requested anomaly set."
            ),
        )

    @staticmethod
    def _annotate_fallback(decision: PlannerDecision, result, context: str) -> PlannerDecision:
        decision.llm_used = False
        decision.llm_transport = getattr(result, "transport_used", "")
        if getattr(result, "error_message", ""):
            decision.llm_error = f"{context}: {result.error_message}"
            decision.llm_error_type = getattr(result, "error_type", "fallback")
        elif result is not None:
            decision.llm_error = f"{context}: LLM result was empty or invalid."
            decision.llm_error_type = getattr(result, "error_type", "fallback") or "fallback"
        return decision

    @staticmethod
    def _parse_selected_anomalies(raw: object) -> List[str]:
        """Normalize LLM selected_anomalies to injectable subtype strings."""
        if not isinstance(raw, list):
            return []
        chosen: List[str] = []
        dict_keys = ("anomaly_subtype", "subtype", "anomaly", "name", "type", "id", "node")
        for item in raw:
            candidate = ""
            if isinstance(item, str):
                candidate = item.strip()
            elif isinstance(item, dict):
                for key in dict_keys:
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        candidate = value.strip()
                        break
            if not candidate:
                continue
            if candidate in SUBTYPE_TO_AGENT:
                subtype = candidate
            elif candidate in CHAIN_NODE_TO_SUBTYPE:
                subtype = CHAIN_NODE_TO_SUBTYPE[candidate]
            elif candidate in CATEGORY_TO_SUBTYPES:
                subtype = CATEGORY_TO_SUBTYPES[candidate][0]
            else:
                continue
            if subtype in SUBTYPE_TO_AGENT and subtype not in chosen:
                chosen.append(subtype)
        return chosen

    @staticmethod
    def _normalize_task_assignments(raw: object, selected: List[str]) -> Dict[str, str]:
        if isinstance(raw, dict):
            return {key: str(value) for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}
        normalized: Dict[str, str] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("anomaly_subtype") or item.get("subtype") or item.get("anomaly") or item.get("name")
                agent = item.get("agent") or item.get("task_agent") or item.get("source_agent") or item.get("agent_type")
                if isinstance(subtype, str) and isinstance(agent, str):
                    normalized[subtype.strip()] = agent.strip()
        if not normalized and len(selected) == 1 and isinstance(raw, str):
            normalized[selected[0]] = raw.strip()
        return normalized

    @staticmethod
    def _normalize_database_mapping(raw: object, selected: List[str]) -> Dict[str, str]:
        if isinstance(raw, dict):
            return {key: str(value) for key, value in raw.items() if isinstance(key, str) and value}
        normalized: Dict[str, str] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("anomaly_subtype") or item.get("subtype") or item.get("anomaly") or item.get("name")
                database = item.get("database") or item.get("target_database") or item.get("execution_database")
                if isinstance(subtype, str) and database:
                    normalized[subtype.strip()] = str(database)
        if not normalized and len(selected) == 1 and isinstance(raw, str):
            normalized[selected[0]] = raw.strip()
        return normalized

    @staticmethod
    def _normalize_task_parameters(raw: object, selected: List[str]) -> Dict[str, Dict[str, object]]:
        if isinstance(raw, dict):
            if len(selected) == 1 and selected[0] not in raw and not any(key in SUBTYPE_TO_AGENT for key in raw):
                return {selected[0]: raw}
            return {key: value if isinstance(value, dict) else {"value": value} for key, value in raw.items() if isinstance(key, str)}
        normalized: Dict[str, Dict[str, object]] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("anomaly_subtype") or item.get("subtype") or item.get("anomaly") or item.get("name")
                params = item.get("parameters") or item.get("task_parameters") or item
                if isinstance(subtype, str):
                    normalized[subtype.strip()] = params if isinstance(params, dict) else {"value": params}
        if not normalized and len(selected) == 1 and isinstance(raw, dict):
            normalized[selected[0]] = raw
        return normalized

    @staticmethod
    def _normalize_string_list(raw: object) -> List[str]:
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

    @staticmethod
    def _normalize_text_field(raw: object) -> str:
        if isinstance(raw, list):
            return json.dumps(raw, ensure_ascii=True)
        if isinstance(raw, dict):
            return json.dumps(raw, ensure_ascii=True)
        return str(raw).strip() if raw is not None else ""

    def _apply_parameter_overrides(self, parameters: Dict[str, Dict[str, object]], request: ExperimentRequest, selected: List[str]) -> Dict[str, Dict[str, object]]:
        overrides = request.user_constraints.get("task_parameter_overrides", {})
        if not isinstance(overrides, dict):
            return parameters
        for subtype in selected:
            update = overrides.get(subtype)
            if isinstance(update, dict):
                parameters.setdefault(subtype, {}).update(update)
        return self._normalize_supported_parameters(parameters, selected)

    @staticmethod
    def _normalize_supported_parameters(parameters: Dict[str, Dict[str, object]], selected: List[str]) -> Dict[str, Dict[str, object]]:
        for subtype in selected:
            params = parameters.setdefault(subtype, {})
            if subtype == "database_table_backup":
                if params.get("source_table"):
                    params["source_table"] = str(params["source_table"])
                if params.get("backup_table"):
                    params["backup_table"] = str(params["backup_table"])
                if "background_duration_seconds" in params:
                    params["background_duration_seconds"] = int(float(params["background_duration_seconds"]))
                if "concurrent_with_probe" in params:
                    params["concurrent_with_probe"] = bool(params["concurrent_with_probe"])
            elif subtype in {"missing_index", "order_by", "group_by", "large_table_scan", "multi_table_join", "implicit_conversion", "excessive_index"}:
                for key in ("background_threads", "repeat"):
                    if key in params:
                        params[key] = int(float(params[key]))
                for key in ("background_sleep",):
                    if key in params:
                        params[key] = float(params[key])
            elif subtype in {"record_lock", "table_lock", "metadata_lock"}:
                for key in ("hold_seconds", "background_duration_seconds"):
                    if key in params:
                        params[key] = int(float(params[key]))
            elif subtype in {"overall_workload", "single_sql"}:
                for key in ("thread_count", "baseline_threads", "repeat"):
                    if key in params:
                        params[key] = int(float(params[key]))
                for key in ("sleep_time", "baseline_sleep"):
                    if key in params:
                        params[key] = float(params[key])
            elif subtype in {"cpu", "io", "network", "memory", "disk"} and "duration_seconds" in params:
                params["duration_seconds"] = int(float(params["duration_seconds"]))
        return parameters

    def _task_agent_input(
        self,
        request: ExperimentRequest,
        context: DBContextSummary,
        planner_context: Dict[str, object],
        decision: PlannerDecision,
        planned_task: PlannedAnomaly,
    ) -> TaskAgentInput:
        memory_context = request.user_constraints.get("planning_memory", {})
        if isinstance(memory_context, PlanningMemoryContext):
            memory_context = asdict(memory_context)
        elif is_dataclass(memory_context):
            memory_context = asdict(memory_context)
        return TaskAgentInput(
            subgoal=planned_task.anomaly_subtype,
            global_context={
                "target_mode": request.target_mode,
                "target_chain": request.target_chain,
                "planner_context": planner_context,
                "global_plan": asdict(decision.global_plan) if decision.global_plan else {},
                "planner_parameters": decision.task_parameters.get(planned_task.anomaly_subtype, {}),
            },
            environment_snapshot={
                "database": context.database,
                "tables": [{"name": table.name, "row_count": table.row_count, "indexes": table.indexes} for table in context.tables],
                "distribution": context.distribution,
            },
            constraints={
                "risk_level": request.risk_level,
                "execution_window_seconds": request.execution_window_seconds,
                **request.safety_constraints,
            },
            expected_effect=planned_task.expected_signals,
            memory=memory_context if isinstance(memory_context, dict) else {},
        )

    def _normalize_categories(self, request: ExperimentRequest) -> List[str]:
        categories = list(request.anomaly_categories)
        for item in request.allowed_anomalies:
            if item in CATEGORY_TO_SUBTYPES and item not in categories:
                categories.append(item)
            elif item in SUBTYPE_TO_AGENT:
                category = self._category_for_subtype(item)
                if category not in categories:
                    categories.append(category)
        return categories

    def _normalize_subtypes(self, request: ExperimentRequest) -> List[str]:
        subtypes = list(request.allowed_subtypes)
        for item in request.allowed_anomalies:
            if item in SUBTYPE_TO_AGENT and item not in subtypes:
                subtypes.append(item)
            elif item in CATEGORY_TO_SUBTYPES:
                for subtype in CATEGORY_TO_SUBTYPES[item]:
                    if subtype not in subtypes:
                        subtypes.append(subtype)
        return subtypes

    @staticmethod
    def _category_for_subtype(subtype: str) -> str:
        for category, members in CATEGORY_TO_SUBTYPES.items():
            if subtype in members:
                return category
        return "slow_sql"

    @staticmethod
    def _database_for_subtype(subtype: str, target_database: str) -> str:
        if subtype in LOCKISH_SUBTYPES:
            if target_database.endswith("_copy"):
                return target_database
            if target_database.endswith("_base"):
                return target_database.replace("_base", "_copy")
            return f"{target_database}_copy"
        return target_database

    def _default_parameters(self, subtype: str, request: ExperimentRequest) -> Dict[str, object]:
        parameters: Dict[str, object] = {"duration_seconds": min(max(request.execution_window_seconds, 1), 20)}
        if self._effective_mode(request) != "single":
            parameters["background_duration_seconds"] = min(max(request.execution_window_seconds + 4, 4), 60)
        if subtype == "overall_workload":
            parameters.update(
                {
                    "baseline_sleep": request.user_constraints.get("baseline_sleep", 0.044),
                    "baseline_threads": request.user_constraints.get("baseline_threads", 80),
                }
            )
        if subtype == "single_sql":
            parameters.update(
                {
                    "baseline_sleep": request.user_constraints.get("baseline_sleep", 0.02),
                    "baseline_threads": request.user_constraints.get("baseline_threads", 12),
                }
            )
        if subtype == "database_table_backup":
            parameters.update({"source_table": request.user_constraints.get("source_table", "orders")})
        return parameters

    @staticmethod
    def _signals_for_subtype(subtype: str) -> List[str]:
        mapping = {
            "record_lock": ["row lock wait", "transaction blocking"],
            "table_lock": ["table lock wait"],
            "metadata_lock": ["metadata lock wait"],
            "single_sql": ["single query latency increase"],
            "overall_workload": ["qps increase", "latency increase"],
            "missing_index": ["full scan or skip scan", "latency increase"],
            "excessive_index": ["write amplification"],
            "implicit_conversion": ["index invalidation", "latency increase"],
            "multi_table_join": ["join explosion", "rows examined increase"],
            "order_by": ["using filesort"],
            "group_by": ["using temporary", "using filesort"],
            "large_table_scan": ["type=ALL", "rows examined increase"],
            "cpu": ["cpu saturation"],
            "io": ["io wait increase"],
            "network": ["network delay"],
            "memory": ["memory pressure"],
            "disk": ["disk pressure"],
            "database_table_backup": ["backup table created"],
        }
        return mapping.get(subtype, ["task-specific validation evidence"])

    @staticmethod
    def _activation_order(selected: List[str]) -> List[str]:
        order_map = {
            "resource_bottleneck": 0,
            "lock_conflict": 1,
            "slow_sql": 2,
            "database_backup": 3,
            "traffic_surge": 4,
        }
        return sorted(selected, key=lambda item: (order_map.get(GlobalPlannerAgent._category_for_subtype(item), 99), item))

    @staticmethod
    def _composite_name(selected: List[str]) -> str:
        return "__".join(selected[:5]) or "single"

    @staticmethod
    def _fallback_summary(request: ExperimentRequest, tasks) -> str:
        anomalies = ", ".join(task.anomaly_type for task in tasks) or "unspecified anomalies"
        return (
            f"Prepare {len(tasks)} task(s) for {anomalies} against {request.target_database or 'the default database'} "
            f"with risk level {request.risk_level}."
        )

    @staticmethod
    def _describe_context(context: DBContextSummary) -> str:
        if not context.tables:
            return "; ".join(context.notes) or "No schema metadata available."
        largest = sorted(context.tables, key=lambda table: table.row_count or 0, reverse=True)[:5]
        parts = [f"{table.name}(rows={table.row_count or 'unknown'}, indexes={len(table.indexes)})" for table in largest]
        if context.notes:
            parts.extend(context.notes)
        return "; ".join(parts)

    @staticmethod
    def _expected_signals(request: ExperimentRequest) -> List[str]:
        signals = ["QPS change", "latency increase", "task-specific validation evidence"]
        combined = request.allowed_anomalies + request.allowed_subtypes + request.anomaly_categories
        if any(item in combined for item in ("cpu", "resource_bottleneck")):
            signals.append("system CPU saturation")
        if any(item in combined for item in ("traffic_surge", "overall_workload", "single_sql")):
            signals.append("workload concurrency increase")
        return signals

    def _global_plan_from_decision(self, request: ExperimentRequest, decision: PlannerDecision) -> GlobalPlan:
        selected = list(decision.selected_anomalies)
        target_chain = list(request.target_chain)
        if target_chain:
            root_causes = [item for item in selected if item in target_chain[:1]] or selected[:1]
            effects = [item for item in target_chain if item not in root_causes]
            dependencies = [[target_chain[i], target_chain[i + 1]] for i in range(len(target_chain) - 1)]
        else:
            root_causes = selected
            effects = ["qps_down", "p95_latency_up"] if request.test_enabled else decision.expected_signals
            dependencies = [[decision.activation_order[i], decision.activation_order[i + 1]] for i in range(len(decision.activation_order) - 1)]
        task_agents = sorted(set(decision.task_assignments.values()))
        evaluation_targets = self._evaluation_targets(request, decision)
        return GlobalPlan(
            mode=request.target_mode or decision.selection_mode,
            root_causes_to_inject=root_causes,
            effects_to_observe=effects,
            task_agents=task_agents,
            task_dependencies=dependencies,
            evaluation_targets=evaluation_targets,
            safety_constraints=request.safety_constraints or {"max_cpu_usage": 90, "max_connection_usage_ratio": 0.8, "max_duration_sec": max(request.execution_window_seconds, 1) * max(request.max_retry_rounds, 1)},
            rationale=decision.selection_rationale or decision.llm_summary,
        )

    @staticmethod
    def _evaluation_targets(request: ExperimentRequest, decision: PlannerDecision) -> List[str]:
        if request.target_chain:
            return list(request.target_chain)
        targets = ["qps_down", "p95_latency_up"]
        for subtype in decision.selected_anomalies:
            if subtype in {"record_lock", "table_lock", "metadata_lock"}:
                targets.append("lock_wait_time_up")
            elif subtype in {"cpu", "io", "memory", "disk", "network"}:
                targets.append("resource_pressure_up")
            elif subtype in {"missing_index", "order_by", "group_by", "large_table_scan", "multi_table_join", "implicit_conversion"}:
                targets.append("slow_query_metric_up")
            elif subtype == "overall_workload":
                targets.append("active_connections_up")
        return sorted(set(targets))
