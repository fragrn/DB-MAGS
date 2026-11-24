"""Session-authored graph contracts over the existing post executor.

Evidence references establish provenance, not independent proof of causality.
The Session interprets observations; graph checks provide temporal constraints.
"""
from pathlib import Path

from db_repro_agent.models.anomaly_graph import AnomalyGraph
from db_repro_agent.graph.validator import AnomalyGraphValidator
from db_repro_agent.evaluation.graph_evaluator import GraphEvaluator
from db_repro_agent.evaluation.node_evaluator import NodeObservation


def validate_spec(payload):
    graph = AnomalyGraph.model_validate(payload.get("graph"))
    AnomalyGraphValidator().assert_valid(graph)
    nodes = {node.node_id for node in graph.nodes}
    injections = payload.get("active_injection_nodes")
    if not isinstance(injections, list) or not injections or not all(isinstance(n, str) and n in nodes for n in injections) or len(set(injections)) != len(injections):
        raise ValueError("active_injection_nodes must be unique graph nodes")
    objectives = payload.get("observation_spec")
    if not isinstance(objectives, dict) or set(objectives) != nodes:
        raise ValueError("observation_spec must cover every graph node")
    if not all(isinstance(v, str) and v.strip() for v in objectives.values()):
        raise ValueError("each observation objective must be non-empty text")
    return {"graph": graph.model_dump(mode="json"), "active_injection_nodes": injections,
            "observation_spec": objectives}


def validate_injections(blueprint, spec):
    if not blueprint.get("task_specs"):
        raise ValueError("propagation requires injection tasks")
    roots = {task.get("metadata", {}).get("root_cause") for task in blueprint["task_specs"]}
    if roots != set(spec["active_injection_nodes"]):
        raise ValueError("TaskSpec root_cause must match active_injection_nodes exactly")


def evaluate_observations(spec, payload, attempt):
    graph = AnomalyGraph.model_validate(spec["graph"])
    entries = payload.get("observations")
    if not isinstance(entries, list):
        raise ValueError("observations must be an array")
    observations = []
    references = {}
    nodes = {node.node_id for node in graph.nodes}
    for item in entries:
        observation = NodeObservation.model_validate(item["observation"])
        if observation.node_id not in nodes or observation.node_id in references:
            raise ValueError("unknown or duplicate observed node")
        refs = item.get("evidence_references")
        if not isinstance(refs, list) or not refs:
            raise ValueError("observations require evidence_references")
        for ref in refs:
            if not isinstance(ref, str):
                raise ValueError("evidence references must be strings")
            path = (attempt / ref).resolve()
            if attempt.resolve() not in path.parents or not path.is_file():
                raise ValueError("evidence must be an existing attempt artifact")
        if not isinstance(observation.hit, bool):
            raise ValueError("Session must explicitly interpret hit; no implicit node rules")
        references[observation.node_id] = refs
        observations.append(observation)
    result = GraphEvaluator().evaluate(graph, observations).model_dump(mode="json")
    missing = sorted(nodes - references.keys())
    result.update(success=result["all_nodes_hit"] and result["all_edges_satisfied"] and not missing,
                  missing_evidence=missing, evidence_references=references,
                  interpretation="Session-authored observations; temporal consistency is not causal proof")
    return result
