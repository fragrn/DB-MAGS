"""Bounded Intent repair scope; execution fields never enter this protocol."""

import copy
import re

from AnomalyGraphAgent.sql_validation import SQLValidationError


class IntentSemanticError(SQLValidationError):
    def __init__(self, source, reason):
        self.source = source
        super().__init__(reason)


def sql_slot(plan, source):
    match = re.fullmatch(r"intents\[(\d+)\]\.sql\.([a-z_]+)", source or "")
    if not match:
        raise ValueError("Repair source is not an Intent SQL slot")
    index, key = int(match[1]), match[2]
    if not 0 <= index < len(plan["intents"]):
        raise ValueError("Repair source index is outside the Intent plan")
    sql = plan["intents"][index]["sql"]
    if not isinstance(sql, dict) or key not in sql:
        raise ValueError("Repair source references a missing SQL slot")
    return index, key


def _references(sql):
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
    try:
        tree = sqlglot.parse_one(sql, read="mysql", error_level=ErrorLevel.RAISE)
        return ({t.name for t in tree.find_all(exp.Table)},
                {c.name.lower() for c in tree.find_all(exp.Column)})
    except sqlglot.errors.ParseError:
        return set(), set()


def guard_sql_repair(before, after, source, whitelist):
    """Allow one SQL slot and only corrections of invalid target references it uses."""
    index, key = sql_slot(before, source)
    corrected = copy.deepcopy(after)
    old, new = before["intents"][index], corrected["intents"][index]
    old_tables, old_columns = _references(old["sql"][key])
    new_tables, new_columns = _references(new["sql"][key])
    target_before, target_after = old["target"] or {}, new["target"] or {}
    if set(target_before) != set(target_after):
        raise ValueError("Repair changed target structure")
    for field in target_before:
        previous, updated = target_before[field], target_after[field]
        if previous == updated:
            continue
        if field == "table":
            allowed = previous not in whitelist and previous in old_tables and updated in whitelist and updated in new_tables
        elif field in {"tables", "columns"}:
            allowed = isinstance(updated, list) and len(previous) == len(updated)
            for left, right in zip(previous, updated):
                if left == right:
                    continue
                if field == "tables":
                    allowed = allowed and left not in whitelist and left in old_tables and right in whitelist and right in new_tables
                else:
                    target_tables = target_after.get("tables", [])
                    known = {c.lower() for t in target_tables for c in whitelist.get(t, [])}
                    allowed = allowed and left.lower() not in known and left.lower() in old_columns and right.lower() in known and right.lower() in new_columns
        else:
            allowed = False
        if not allowed:
            raise ValueError(f"Repair changed protected target.{field}")
        target_after[field] = copy.deepcopy(previous)
    new["sql"][key] = old["sql"][key]
    if corrected != before:
        raise ValueError("Repair changed protected Intent fields or another SQL slot")


def guard_semantic_repair(before, after, source, whitelist):
    if ".sql." in source:
        return guard_sql_repair(before, after, source, whitelist)
    match = re.fullmatch(r"intents\[(\d+)\]\.target\.(table|tables|columns)", source or "")
    if not match:
        raise ValueError("No permitted semantic repair field")
    index, field = int(match[1]), match[2]
    old_target, new_target = before["intents"][index]["target"], after["intents"][index]["target"]
    if field == "table":
        if old_target[field] in whitelist:
            raise ValueError("Cannot move a valid target table")
    else:
        old_values, new_values = old_target[field], new_target[field]
        known = (set(whitelist) if field == "tables" else
                 {c.lower() for t in old_target.get("tables", []) for c in whitelist.get(t, [])})
        if len(old_values) != len(new_values):
            raise ValueError("Cannot add or remove target references during repair")
        for old, new in zip(old_values, new_values):
            if old != new and (old if field == "tables" else old.lower()) in known:
                raise ValueError("Cannot change an already valid target reference")
    corrected = copy.deepcopy(after)
    corrected["intents"][index]["target"][field] = before["intents"][index]["target"][field]
    if corrected != before:
        raise ValueError("Semantic repair changed fields outside the failed target reference")


def guard_partial_repair(before, candidate):
    """Reject identifiable scope changes even when the rest of a repair is malformed."""
    if not isinstance(candidate, dict) or not isinstance(candidate.get("intents"), list):
        return
    expected = {item["node"]: item for item in before["intents"]}
    items = candidate["intents"]
    if len(items) != len(expected):
        raise ValueError("Repair changed the number of Intents")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or "node" not in item:
            continue
        node = item["node"]
        if not isinstance(node, str) or node not in expected or node in seen:
            raise ValueError("Repair changed or duplicated a root")
        seen.add(node)
        for field in ("strategy", "load", "expected_evidence"):
            if field in item and item[field] != expected[node][field]:
                raise ValueError(f"Repair changed protected {field}")
        if isinstance(item.get("sql"), dict):
            if set(item["sql"]) - set(expected[node]["sql"] or {}):
                raise ValueError("Repair added SQL slots")
