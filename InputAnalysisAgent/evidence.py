"""Resolve predeclared evidence selectors against the current execution bundle."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from InputAnalysisAgent.hitl import write_json


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def freeze_spec(directory: Path, spec: dict) -> None:
    path = directory / "evaluation_contract.json"
    if path.exists():
        verify_spec(directory, spec)
    else:
        write_json(path, {"spec": spec, "sha256": digest(spec)})


def verify_spec(directory: Path, spec: dict) -> None:
    frozen = json.loads((directory / "evaluation_contract.json").read_text())
    if frozen["sha256"] != digest(spec) or frozen["sha256"] != digest(frozen["spec"]):
        raise ValueError("evaluation criteria changed after execution began")


def capture_bundle(directory: Path, execution: dict, *, include_explain: bool = True) -> dict:
    """Keep calibration and preparation evidence separate from actual execution."""
    bundle = dict(execution)
    for name in ("preparation_result", "calibration_result", "explain_result"):
        if name == "explain_result" and not include_explain:
            continue
        path = directory / f"{name}.json"
        if path.exists():
            bundle[name] = json.loads(path.read_text())
    actions, events = {}, {}
    for task_id, task in execution.get("execution_trace", {}).get("tasks", {}).items():
        outputs = task.get("actions")
        if outputs is None:
            outputs = []
            for line in task.get("stdout", "").splitlines():
                if line.startswith("{"):
                    try:
                        result = ast.literal_eval(line)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(result, dict):
                        outputs.append(result)
        actions[task_id] = outputs
        events[task_id] = [
            {"mysql_error_code": int(m[1]), "driver_error": error}
            for error in task.get("errors", [])
            for m in re.finditer(r"\((\d{4}), ['\"]", error)
        ]
    bundle.update(execution_actions=actions, execution_events=events)
    return bundle


def seal_bundle(directory: Path, bundle: dict) -> None:
    write_json(directory / "evaluation_bundle_digest.json", {"sha256": digest(bundle)})


def verify_bundle(directory: Path, bundle: dict) -> None:
    receipt = json.loads((directory / "evaluation_bundle_digest.json").read_text())
    if receipt["sha256"] != digest(bundle):
        raise ValueError("execution evidence changed after capture")


def _resolve(document: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(document, list):
            if not part.isascii() or not part.isdigit():
                raise KeyError(part)
            document = document[int(part)]
        else:
            document = document[part]
    return document


def collect(spec: dict, bundle: dict, *, attempt_id: str) -> dict:
    """Only index values requested by the frozen contract, not arbitrary files."""
    prefix = digest([attempt_id, digest(spec), digest(bundle)])[:16]
    evidence = {}
    for criterion in spec["criteria"]:
        for name, pointer in criterion["inputs"].items():
            evidence_id = f"{prefix}:{criterion['id']}:{name}"
            entry = {"criterion_id": criterion["id"], "input": name,
                     "artifact": "execution_bundle.json", "pointer": pointer,
                     "attempt_id": attempt_id}
            section = pointer.split("/")[1]
            entry["phase"] = {
                "preparation_result": "preparation", "explain_result": "calibration",
                "baseline": "baseline", "after": "post_execution",
            }.get(section, "execution")
            try:
                value = _resolve(bundle, pointer)
                entry.update(available=value is not None and value not in ("", [], {}), value=value)
            except (KeyError, IndexError, TypeError):
                entry.update(available=False, value=None)
            evidence[evidence_id] = entry
    return evidence
