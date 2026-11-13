"""Deterministic verdicts over rule checks and evidence-bound semantic judgments."""

from __future__ import annotations

import math
import operator
import re
from dataclasses import dataclass

from InputAnalysisAgent.evidence import collect


DIMENSIONS = ("symptom", "mechanism", "environment")
COMPARISONS = {">=": operator.ge, ">": operator.gt, "<=": operator.le,
               "<": operator.lt, "==": operator.eq, "!=": operator.ne}


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def validate_spec(spec: dict) -> None:
    if type(spec.get("version")) is not int or spec["version"] != 2:
        raise ValueError("unsupported evaluation_spec.version")
    criteria = spec.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("evaluation_spec.criteria must be a non-empty array")
    ids = set()
    required = set()
    for c in criteria:
        if not isinstance(c, dict):
            raise ValueError("evaluation criterion must be an object")
        cid, dim, kind = c.get("id"), c.get("dimension"), c.get("kind")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", cid) or cid in ids:
            raise ValueError("criterion ids must be unique identifiers")
        ids.add(cid)
        if dim not in DIMENSIONS or kind not in ("numeric", "event", "semantic"):
            raise ValueError(f"invalid dimension or kind for {cid}")
        if type(c.get("required")) is not bool or any(
            not isinstance(c.get(k), str) or not c[k].strip() for k in ("description", "post_basis")
        ):
            raise ValueError(f"criterion {cid} requires required, description and post_basis")
        if c["required"]:
            required.add(dim)
        inputs = c.get("inputs")
        if not isinstance(inputs, dict) or not inputs or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or not isinstance(pointer, str) or not re.fullmatch(r"(?:/(?:[^~/]|~[01])*)+", pointer)
            for name, pointer in inputs.items()
        ):
            raise ValueError(f"criterion {cid} needs named execution bundle JSON pointers")
        if kind == "numeric":
            operation = c.get("operation", "value")
            expected = {"target"} if operation == "value" else {"target", "control"}
            if (operation not in ("value", "ratio", "delta") or set(inputs) != expected
                    or not isinstance(c.get("operator"), str) or c["operator"] not in COMPARISONS
                    or not _number(c.get("threshold"))
                    or not isinstance(c.get("threshold_source"), str) or not c["threshold_source"].strip()):
                raise ValueError(f"invalid numeric criterion {cid}")
        if kind == "event" and (set(inputs) != {"target"} or type(c.get("expected")) not in (str, int, bool)):
            raise ValueError(f"event {cid} needs a target and exact scalar expected value")
        sources = {p.split("/")[1] for p in inputs.values()}
        allowed = {"baseline", "after", "execution_actions", "execution_events",
                   "execution_trace", "slow_log_evidence", "explain_result", "preparation_result"}
        if sources - allowed:
            raise ValueError(f"criterion {cid} points outside supported evidence sections")
        if dim == "symptom" and sources & {"explain_result", "preparation_result"}:
            raise ValueError(f"symptom {cid} must use runtime observations")
    if required != set(DIMENSIONS):
        raise ValueError("each dimension needs at least one required criterion")


@dataclass
class EvidenceEvaluation:
    result: dict

    @property
    def success(self) -> bool:
        return self.result["success"]

    def to_dict(self) -> dict:
        return self.result


def evaluate(spec: dict, bundle: dict, judgments: dict, *, attempt_id: str) -> EvidenceEvaluation:
    validate_spec(spec)
    evidence = collect(spec, bundle, attempt_id=attempt_id)
    semantic_ids = {c["id"] for c in spec["criteria"] if c["kind"] == "semantic"}
    if not isinstance(judgments, dict) or set(judgments) - semantic_ids:
        raise ValueError("judgments must refer only to declared semantic criteria")
    rows = []
    for c in spec["criteria"]:
        ids = {eid for eid, e in evidence.items() if e["criterion_id"] == c["id"] and e["available"]}
        values = {evidence[eid]["input"]: evidence[eid]["value"] for eid in ids}
        status, reason, observed = "unknown", "Required evidence is missing", None
        refs = sorted(ids)
        if c["kind"] == "semantic":
            j = judgments.get(c["id"])
            reason = "No semantic judgment submitted" if j is None else reason
            if j is not None:
                if (not isinstance(j, dict) or j.get("status") not in ("hit", "miss", "unknown")
                        or not isinstance(j.get("reason"), str) or not j["reason"].strip()
                        or not isinstance(j.get("evidence_ids"), list)
                        or any(not isinstance(x, str) or x not in ids for x in j["evidence_ids"])):
                    raise ValueError(f"invalid or out-of-scope evidence judgment for {c['id']}")
                refs = j["evidence_ids"]
                if len(ids) == len(c["inputs"]) and set(refs) == ids:
                    status, reason = j["status"], j["reason"]
                elif j["status"] == "unknown":
                    reason = j["reason"]
        elif len(values) == len(c["inputs"]):
            target = values["target"]
            if c["kind"] == "event":
                observed = target
                expected = c["expected"]
                if type(target) is type(expected):
                    status = "hit" if target == expected else "miss"
                    reason = "Exact event comparison"
                else:
                    reason = "Event evidence has an incompatible type"
            else:
                control = values.get("control")
                operation = c.get("operation", "value")
                if _number(target) and (operation == "value" or _number(control)):
                    if operation == "value":
                        observed = target
                    elif operation == "delta":
                        observed = target - control
                    elif control > 0:
                        observed = target / control
                if _number(observed):
                    status = "hit" if COMPARISONS[c["operator"]](observed, c["threshold"]) else "miss"
                    reason = "Computed from recorded evidence"
                else:
                    observed = None
                    reason = "Invalid metric or nonpositive ratio denominator"
        rows.append({"id": c["id"], "dimension": c["dimension"], "required": c["required"],
                     "description": c["description"], "post_basis": c["post_basis"],
                     "status": status, "reason": reason, "observed": observed, "evidence_ids": refs,
                     **{k: c[k] for k in ("operation", "operator", "threshold", "threshold_source", "expected")
                        if k in c}})
    dimensions = {}
    for dim in DIMENSIONS:
        states = [r["status"] for r in rows if r["dimension"] == dim and r["required"]]
        dimensions[dim] = "miss" if "miss" in states else "unknown" if "unknown" in states else "hit"
    success = all(status == "hit" for status in dimensions.values())
    gaps = [r for r in rows if r["required"] and r["status"] != "hit"]
    # A matching environment alone does not demonstrate partial reproduction.
    positive = any(r["status"] == "hit" and r["required"] and r["dimension"] != "environment" for r in rows)
    outcome = "success" if success else "partial" if positive else (
        "not_reproduced" if "miss" in dimensions.values() else "inconclusive")
    return EvidenceEvaluation({"version": 2, "criteria": rows, "dimensions": dimensions,
                               "symptom_hit": dimensions["symptom"] == "hit",
                               "mechanism_hit": dimensions["mechanism"] == "hit",
                               "success": success, "outcome": outcome,
                               "reason": "; ".join(f"{dim}={status}" for dim, status in dimensions.items()),
                               "reproduction_level": "mechanism" if success else "symptom_only"
                               if dimensions["symptom"] == "hit" else None,
                               "evidence": evidence, "reflection_feedback": gaps})
