"""Batch orchestration for post-driven reproductions."""

from __future__ import annotations

import json
import re
import signal
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from InputAnalysisAgent.db_adapters import normalize_dbms, supported_execution_dbms
from InputAnalysisAgent.hitl import HumanDecision, HumanGateRequired, load_state, save_state, write_json
from InputAnalysisAgent.runtime import ReproductionRuntime
from InputAnalysisAgent.schemas import ReproductionBlueprint


BatchStatus = Literal["success", "partial", "blocked", "abandoned", "failed"]


@dataclass
class AttemptResult:
    attempt: int
    run_dir: str | None
    status: BatchStatus
    reason: str
    evaluation: dict[str, Any] | None = None
    repairs: list[dict[str, Any]] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "run_dir": self.run_dir,
            "status": self.status,
            "reason": self.reason,
            "evaluation": self.evaluation,
            "repairs": self.repairs,
            "missing_capabilities": self.missing_capabilities,
        }


@dataclass
class PostResult:
    input_file: str
    category: str
    slug: str
    status: BatchStatus
    reason: str
    attempts: list[AttemptResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_file": self.input_file,
            "category": self.category,
            "slug": self.slug,
            "status": self.status,
            "reason": self.reason,
            "attempts": [item.to_dict() for item in self.attempts],
        }


def run_batch(
    *,
    input_root: str | Path,
    output_root: str | Path,
    max_attempts: int = 4,
    attempt_timeout_sec: int = 300,
    limit: int | None = None,
    runtime: ReproductionRuntime | None = None,
) -> dict[str, Any]:
    input_root = Path(input_root)
    output_root = Path(output_root)
    runtime = runtime or ReproductionRuntime()
    posts = sorted(path for path in input_root.rglob("*.txt") if path.is_file())
    if limit is not None:
        posts = posts[: max(0, limit)]
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[PostResult] = []
    unsupported: list[dict[str, Any]] = []
    for post_path in posts:
        result = _run_one_post(runtime, input_root, output_root, post_path, max_attempts, attempt_timeout_sec)
        results.append(result)
        for attempt in result.attempts:
            for capability in attempt.missing_capabilities:
                unsupported.append(
                    {
                        "input_file": str(post_path),
                        "category": result.category,
                        "slug": result.slug,
                        "attempt": attempt.attempt,
                        "missing_capability": capability,
                        "reason": attempt.reason,
                    }
                )
        _write_summary(output_root, results, unsupported)
    return _write_summary(output_root, results, unsupported)


def _run_one_post(
    runtime: ReproductionRuntime,
    input_root: Path,
    output_root: Path,
    post_path: Path,
    max_attempts: int,
    attempt_timeout_sec: int,
) -> PostResult:
    rel = post_path.relative_to(input_root)
    category = rel.parts[0] if len(rel.parts) > 1 else "uncategorized"
    slug = _slug(post_path.stem)
    post_dir = output_root / category / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[AttemptResult] = []
    for attempt_no in range(1, max(1, max_attempts) + 1):
        attempt_root = post_dir / f"attempt_{attempt_no}"
        attempt_root.mkdir(parents=True, exist_ok=True)
        database = _database_name(category, slug, attempt_no)
        result = _run_attempt_with_timeout(runtime, post_path, attempt_root, database, attempt_no, attempt_timeout_sec)
        attempts.append(result)
        _write_json(post_dir / "post_result.json", _finalize(post_path, category, slug, attempts).to_dict())
        if _no_progress_planning_timeouts(attempts) >= 2:
            break
        if result.status in {"success", "blocked", "abandoned"}:
            break
    final = _finalize(post_path, category, slug, attempts)
    _write_json(post_dir / "post_result.json", final.to_dict())
    return final


class BatchAttemptTimeout(BaseException):
    pass


def _run_attempt_with_timeout(
    runtime: ReproductionRuntime,
    post_path: Path,
    attempt_root: Path,
    database: str,
    attempt_no: int,
    timeout_sec: int,
) -> AttemptResult:
    if timeout_sec <= 0:
        return _run_attempt(runtime, post_path, attempt_root, database, attempt_no)

    def _timeout(_signum: int, _frame: Any) -> None:
        raise BatchAttemptTimeout(f"attempt timed out after {timeout_sec}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(timeout_sec)
    try:
        return _run_attempt(runtime, post_path, attempt_root, database, attempt_no)
    except BatchAttemptTimeout as exc:
        run_dir = _latest_run_dir(attempt_root)
        if run_dir:
            if (run_dir / "state.json").exists():
                state = load_state(run_dir)
                state.status = "failed"
                state.last_error = str(exc)
                save_state(run_dir, state)
            _write_json(
                run_dir / "batch_attempt_timeout.json",
                {"status": "failed", "reason": str(exc), "timeout_sec": timeout_sec, "timestamp": time.time()},
            )
            _write_json(
                run_dir / "failure.json",
                {
                    "phase": "planning",
                    "error": str(exc),
                    "exception_type": "BatchAttemptTimeout",
                    "timestamp": time.time(),
                },
            )
        return AttemptResult(attempt_no, str(run_dir) if run_dir else None, "failed", str(exc))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _run_attempt(
    runtime: ReproductionRuntime,
    post_path: Path,
    attempt_root: Path,
    database: str,
    attempt_no: int,
) -> AttemptResult:
    precheck = _precheck_blocked(post_path, attempt_root, attempt_no)
    if precheck is not None:
        return precheck
    original_post = post_path.read_text(errors="replace")
    planner_post, compaction = _compact_post_for_planning(original_post)
    _write_json(attempt_root / "input_compaction.json", compaction)
    (attempt_root / "source_post.txt").write_text(original_post, encoding="utf-8")
    (attempt_root / "planner_post.txt").write_text(planner_post, encoding="utf-8")
    metadata = {
        "batch": True,
        "source_file": str(post_path),
        "target_database": database,
        "attempt": attempt_no,
        "instruction": f"Use this dedicated test database for all setup and task actions: {database}.",
        "input_compaction": compaction,
    }
    try:
        result = runtime.run(planner_post, metadata=metadata, output_root=str(attempt_root), interaction="checkpoint")
        return _completed_attempt(attempt_no, Path(result["run_dir"]), [])
    except HumanGateRequired as gate:
        return _handle_gate(runtime, gate.run_dir, database, attempt_no)
    except Exception as exc:
        run_dir = _latest_run_dir(attempt_root)
        if not run_dir:
            return AttemptResult(attempt_no, None, "failed", str(exc))
        repairs = _repair_run_blueprint(run_dir, database)
        if repairs:
            try:
                result = runtime.resume(run_dir, HumanDecision("retry", actor="batch"))
                return _completed_attempt(attempt_no, Path(result["run_dir"]), repairs)
            except HumanGateRequired as gate:
                return _handle_gate(runtime, gate.run_dir, database, attempt_no, repairs=repairs)
            except Exception as retry_exc:
                return AttemptResult(
                    attempt_no,
                    str(run_dir),
                    _failure_status(run_dir),
                    str(retry_exc),
                    repairs=repairs,
                    missing_capabilities=_missing_capabilities(run_dir),
                )
        return AttemptResult(
            attempt_no,
            str(run_dir),
            _failure_status(run_dir),
            str(exc),
            missing_capabilities=_missing_capabilities(run_dir),
        )


def _precheck_blocked(post_path: Path, attempt_root: Path, attempt_no: int) -> AttemptResult | None:
    text = post_path.read_text(errors="replace")
    dbms, evidence = _infer_unsupported_dbms(text)
    if not dbms:
        return None
    if normalize_dbms(dbms) in supported_execution_dbms():
        return None
    run_dir = attempt_root / "precheck_blocked"
    run_dir.mkdir(parents=True, exist_ok=True)
    missing = [f"{dbms} execution adapter"]
    reason = f"Current InputAnalysisAgent v1 does not have an execution adapter for detected {dbms} posts."
    payload = {
        "status": "blocked",
        "dbms": dbms,
        "reason": reason,
        "evidence": evidence,
        "missing_capabilities": missing,
        "source_file": str(post_path),
        "timestamp": time.time(),
    }
    _write_json(run_dir / "precheck_blocked.json", payload)
    _write_json(
        run_dir / "state.json",
        {
            "run_id": "precheck_blocked",
            "status": "failed",
            "phase": "precheck",
            "interaction": "checkpoint",
            "completed_phases": [],
            "artifacts": {"precheck_blocked": "precheck_blocked.json"},
            "failed_rounds": 0,
            "calibration_failed_rounds": 0,
            "evaluation_failed_rounds": 0,
            "pending_gate": None,
            "last_error": reason,
            "updated_at": time.time(),
        },
    )
    return AttemptResult(attempt_no, str(run_dir), "blocked", reason, missing_capabilities=missing)


def _infer_unsupported_dbms(text: str) -> tuple[str | None, list[str]]:
    lowered = text.lower()
    signals: list[tuple[str, str]] = []
    patterns = {
        "sqlserver": [
            r"\bsql server\b",
            r"\bssms\b",
            r"\bsys\.dm_",
            r"\bresource_semaphore\b",
            r"\bexecution plan\b.*\bsql server\b",
            r"\bsp_blitzcache\b",
            r"\bwait_info\b",
            r"\btempdb\b",
        ],
    }
    for dbms, dbms_patterns in patterns.items():
        for pattern in dbms_patterns:
            if re.search(pattern, lowered, re.S):
                signals.append((dbms, pattern))
    if not signals:
        return None, []
    has_mysql = bool(re.search(r"\bmysql\b|\binnodb\b|\bmariadb\b|\bpercona\b", lowered))
    counts: dict[str, int] = {}
    for dbms, _pattern in signals:
        counts[dbms] = counts.get(dbms, 0) + 1
    dbms = max(counts, key=counts.get)
    if has_mysql and counts[dbms] <= 1:
        return None, []
    return dbms, [pattern for found_dbms, pattern in signals if found_dbms == dbms]


def _compact_post_for_planning(text: str, *, max_chars: int = 14000) -> tuple[str, dict[str, Any]]:
    """Keep incident evidence while removing fetched-link noise that hurts LLM reliability."""
    original_len = len(text)
    compacted = re.split(r"\n# Linked Page Snapshots\b", text, maxsplit=1)[0].rstrip()
    removed_link_snapshots = len(compacted) != original_len
    if len(compacted) > max_chars:
        head = int(max_chars * 0.65)
        tail = max_chars - head
        compacted = (
            compacted[:head].rstrip()
            + "\n\n[... middle of long forum export omitted by batch planner compaction ...]\n\n"
            + compacted[-tail:].lstrip()
        )
    if compacted != text:
        compacted += (
            "\n\n[Batch planner note: the original full post is saved as source_post.txt. "
            "This planning input was compacted to remove linked-page snapshots or excessive length.]"
        )
    return compacted, {
        "original_chars": original_len,
        "planner_chars": len(compacted),
        "removed_link_snapshots": removed_link_snapshots,
        "max_chars": max_chars,
    }


def _handle_gate(
    runtime: ReproductionRuntime,
    run_dir: Path,
    database: str,
    attempt_no: int,
    *,
    repairs: list[dict[str, Any]] | None = None,
) -> AttemptResult:
    repairs = list(repairs or [])
    for step in range(1, 7):
        repairs.extend(_repair_run_blueprint(run_dir, database))
        blueprint = _read_json(run_dir / "blueprint.json") if (run_dir / "blueprint.json").exists() else {}
        gate = _read_json(run_dir / "hitl_request.json") if (run_dir / "hitl_request.json").exists() else {}
        decision = _gate_decision(gate, blueprint, step)
        write_json(run_dir / f"batch_gate_decision_{step}.json", {"gate": gate, "decision": decision.to_dict(), "repairs": repairs})
        if decision.decision == "reject":
            return AttemptResult(
                attempt_no,
                str(run_dir),
                "blocked" if _is_blocked(blueprint) else "abandoned",
                _blocked_reason(blueprint) or "Batch policy rejected gated reproduction.",
                repairs=repairs,
                missing_capabilities=_missing_capabilities(run_dir, blueprint),
            )
        try:
            result = runtime.resume(run_dir, decision)
            return _completed_attempt(attempt_no, Path(result["run_dir"]), repairs)
        except HumanGateRequired:
            continue
        except Exception as exc:
            repairs.extend(_repair_run_blueprint(run_dir, database))
            if repairs:
                try:
                    result = runtime.resume(run_dir, HumanDecision("retry", actor="batch"))
                    return _completed_attempt(attempt_no, Path(result["run_dir"]), repairs)
                except HumanGateRequired:
                    continue
                except Exception as retry_exc:
                    return AttemptResult(
                        attempt_no,
                        str(run_dir),
                        _failure_status(run_dir),
                        str(retry_exc),
                        repairs=repairs,
                        missing_capabilities=_missing_capabilities(run_dir),
                    )
            return AttemptResult(
                attempt_no,
                str(run_dir),
                _failure_status(run_dir),
                str(exc),
                repairs=repairs,
                missing_capabilities=_missing_capabilities(run_dir),
            )
    return AttemptResult(attempt_no, str(run_dir), "abandoned", "Still gated after automated handling.", repairs=repairs)


def _gate_decision(gate: dict[str, Any], blueprint: dict[str, Any], step: int) -> HumanDecision:
    if _is_blocked(blueprint):
        return HumanDecision("reject", actor="batch")
    reasons = set(gate.get("reasons") or [])
    if "high_risk" in reasons or "privileged_or_global_operation" in reasons:
        return HumanDecision("approve", actor="batch") if _global_changes_have_cleanup(blueprint) else HumanDecision("reject", actor="batch")
    if "calibration_rejected_by_llm" in reasons and step <= 2:
        return HumanDecision(
            "feedback",
            actor="batch",
            feedback=(
                "Regenerate a simpler executable reproduction using bounded synthetic data, "
                "valid executor action schema, clear EXPLAIN calibration, and no unsupported DBMS-specific tools."
            ),
        )
    return HumanDecision("approve", actor="batch")


def _repair_run_blueprint(run_dir: Path, database: str) -> list[dict[str, Any]]:
    source = run_dir / "blueprint.json"
    if not source.exists():
        source = run_dir / "candidate_blueprint.json"
    if not source.exists():
        return []
    payload = _read_json(source)
    repaired, changes = repair_blueprint_payload(payload, database)
    if not changes:
        return []
    write_json(run_dir / "blueprint_repair_log.json", {"changes": changes, "timestamp": time.time()})
    write_json(run_dir / "blueprint.json", repaired)
    try:
        blueprint = ReproductionBlueprint.from_dict(repaired)
    except Exception:
        write_json(run_dir / "blueprint_repair_unvalidated.json", repaired)
        return changes
    ReproductionRuntime._write_blueprint_artifacts(run_dir, blueprint)
    if (run_dir / "state.json").exists():
        state = load_state(run_dir)
        if "planning" not in state.completed_phases:
            state.completed_phases.append("planning")
        save_state(run_dir, state)
    return changes


def repair_blueprint_payload(payload: dict[str, Any], database: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = json.loads(json.dumps(payload))
    changes: list[dict[str, Any]] = []

    def record(path: str, before: Any, after: Any, reason: str) -> None:
        if before != after:
            changes.append({"path": path, "before": before, "after": after, "reason": reason})

    def normalize_confidence(item: Any, path: str = "") -> None:
        if isinstance(item, dict):
            for key, child in list(item.items()):
                child_path = f"{path}.{key}" if path else key
                if key == "confidence" and child is None:
                    item[key] = 0.5
                    record(child_path, None, 0.5, "Default missing confidence.")
                else:
                    normalize_confidence(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                normalize_confidence(child, f"{path}[{index}]")

    normalize_confidence(value)
    for root, key in (("environment_spec", "database"), ("data_spec", "database"), ("experiment_request", "target_database")):
        obj = value.setdefault(root, {})
        before = obj.get(key)
        obj[key] = database
        record(f"{root}.{key}", before, database, "Use per-post attempt database.")
    data = value.setdefault("data_spec", {})
    calibrations = data.get("calibration_queries")
    if isinstance(calibrations, list):
        before = json.loads(json.dumps(calibrations))
        data["calibration_queries"] = [
            item for item in calibrations
            if isinstance(item, dict) and re.match(r"^\s*(SELECT|WITH)\b", str(item.get("sql") or ""), re.I)
        ]
        record(
            "data_spec.calibration_queries",
            before,
            data["calibration_queries"],
            "Remove calibration queries that cannot be EXPLAINed as read-only SELECT/WITH.",
        )
        before_placeholders = json.loads(json.dumps(data["calibration_queries"]))
        for item in data["calibration_queries"]:
            if isinstance(item, dict) and isinstance(item.get("sql"), str):
                item["sql"] = _replace_unbound_placeholders(item["sql"])
        record(
            "data_spec.calibration_queries",
            before_placeholders,
            data["calibration_queries"],
            "Replace unresolved SQL placeholders with synthetic literals for executable calibration.",
        )
    if isinstance(data.get("tables"), list) and all(isinstance(item, str) for item in data["tables"]):
        before = data["tables"]
        data["tables"] = [{"name": item, "purpose": "synthetic reproduction table", "target_rows": 1000, "distribution_notes": ""} for item in before]
        record("data_spec.tables", before, data["tables"], "Convert table names to objects.")
    if not isinstance(data.get("constraints"), dict):
        before = data.get("constraints")
        data["constraints"] = {"notes": before}
        record("data_spec.constraints", before, data["constraints"], "Constraints must be an object.")
    scale = data.get("scale_strategy")
    if not isinstance(scale, dict):
        before = scale
        data["scale_strategy"] = {"initial_rows": 1000, "max_rows": 10000, "growth_factor": 2.0, "max_rounds": 2}
        record("data_spec.scale_strategy", before, data["scale_strategy"], "Add valid scale strategy.")
    else:
        before = dict(scale)
        scale["initial_rows"] = max(1, int(scale.get("initial_rows") or 1000))
        scale["max_rows"] = max(scale["initial_rows"], int(scale.get("max_rows") or scale["initial_rows"]))
        scale["growth_factor"] = float(scale.get("growth_factor") or 2.0)
        if scale["growth_factor"] <= 1:
            scale["growth_factor"] = 2.0
        scale["max_rounds"] = max(1, int(scale.get("max_rounds") or 1))
        record("data_spec.scale_strategy", before, scale, "Normalize scale strategy.")
    if isinstance(data.get("analyze_tables"), list):
        before = list(data["analyze_tables"])
        data["analyze_tables"] = [str(item).split(".")[-1].strip("`") for item in before if str(item).strip()]
        record("data_spec.analyze_tables", before, data["analyze_tables"], "Use unqualified table names.")
    tasks = value.get("task_specs")
    if isinstance(tasks, list):
        before_tasks = json.loads(json.dumps(tasks))
        removed_task_ids: set[str] = set()
        kept_tasks: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or "")
            actions = task.get("actions") or []
            kinds = {str(action.get("kind") or "").lower() for action in actions if isinstance(action, dict)}
            task_type = str(task.get("task_type") or "").lower()
            if kinds and kinds <= _SETUP_ONLY_ACTION_KINDS:
                if task_id:
                    removed_task_ids.add(task_id)
                continue
            if task_type in _SETUP_ONLY_TASK_TYPES and not actions:
                if task_id:
                    removed_task_ids.add(task_id)
                continue
            kept_tasks.append(task)
        if removed_task_ids:
            value["task_specs"] = kept_tasks
            record(
                "task_specs",
                before_tasks,
                kept_tasks,
                "Remove database/schema setup TaskSpecs; runtime prepares the dedicated database from DataSpec.",
            )
            deps = value.get("dependencies")
            if isinstance(deps, list):
                before_deps = json.loads(json.dumps(deps))
                value["dependencies"] = [
                    dep for dep in deps
                    if not _dependency_mentions_removed_task(dep, removed_task_ids)
                ]
                record(
                    "dependencies",
                    before_deps,
                    value["dependencies"],
                    "Remove dependencies attached to setup-only TaskSpecs.",
                )
    workload = value.setdefault("workload_spec", {})
    queries = workload.get("queries")
    if isinstance(queries, list) and any(isinstance(item, dict) for item in queries):
        before = json.loads(json.dumps(queries))
        workload["queries"] = [str(item.get("sql") or "").strip() if isinstance(item, dict) else str(item) for item in queries]
        workload["queries"] = [query for query in workload["queries"] if query]
        record("workload_spec.queries", before, workload["queries"], "Runtime SQL background workload expects query strings.")
    if isinstance(workload.get("queries"), list):
        before = list(workload["queries"])
        workload["queries"] = [_replace_unbound_placeholders(str(query)) for query in workload["queries"]]
        record("workload_spec.queries", before, workload["queries"], "Replace unresolved SQL placeholders with synthetic literals.")
    request = value.setdefault("experiment_request", {})
    try:
        max_duration = float(request.get("max_duration_sec") or 60)
    except (TypeError, ValueError):
        max_duration = 60.0
        before = request.get("max_duration_sec")
        request["max_duration_sec"] = max_duration
        record("experiment_request.max_duration_sec", before, max_duration, "Normalize invalid request duration.")
    for task_index, task in enumerate(value.get("task_specs") or []):
        for action_key in ("actions", "cleanup_actions"):
            for action_index, action in enumerate(task.get(action_key) or []):
                if not isinstance(action, dict):
                    continue
                if "argv" in action and "command" not in action:
                    before = action.pop("argv")
                    action["command"] = before
                    record(f"task_specs[{task_index}].{action_key}[{action_index}].command", {"argv": before}, before, "Use command field.")
                if action.get("database"):
                    before = action["database"]
                    action["database"] = database
                    record(f"task_specs[{task_index}].{action_key}[{action_index}].database", before, database, "Use per-post attempt database.")
                for sql_key in ("sql",):
                    if isinstance(action.get(sql_key), str):
                        before = action[sql_key]
                        action[sql_key] = _replace_unbound_placeholders(before)
                        record(f"task_specs[{task_index}].{action_key}[{action_index}].{sql_key}", before, action[sql_key], "Replace unresolved SQL placeholders with synthetic literals.")
                for script_index, script in enumerate(action.get("scripts") or []):
                    if not isinstance(script, dict):
                        continue
                    for step_index, step in enumerate(script.get("steps") or []):
                        if isinstance(step, dict) and isinstance(step.get("sql"), str):
                            before = step["sql"]
                            step["sql"] = _replace_unbound_placeholders(before)
                            record(
                                f"task_specs[{task_index}].{action_key}[{action_index}].scripts[{script_index}].steps[{step_index}].sql",
                                before,
                                step["sql"],
                                "Replace unresolved SQL placeholders with synthetic literals.",
                            )
                duration = action.get("duration_sec")
                try:
                    duration_value = float(duration)
                except (TypeError, ValueError):
                    duration_value = 1.0
                    before = duration
                    action["duration_sec"] = duration_value
                    record(f"task_specs[{task_index}].{action_key}[{action_index}].duration_sec", before, duration_value, "Normalize missing action duration.")
                    continue
                if duration_value > max_duration and action_key == "actions":
                    before = duration
                    action["duration_sec"] = max_duration
                    record(
                        f"task_specs[{task_index}].{action_key}[{action_index}].duration_sec",
                        before,
                        max_duration,
                        "Cap action duration to experiment_request.max_duration_sec.",
                    )
    return value, changes


_SETUP_ONLY_ACTION_KINDS = {
    "create_database",
    "create_schema",
    "setup_database",
    "prepare_database",
    "data_prep",
}
_SETUP_ONLY_TASK_TYPES = {
    "create_database",
    "create_schema",
    "setup_database",
    "prepare_database",
    "data_prep",
}


def _dependency_mentions_removed_task(dep: Any, removed_task_ids: set[str]) -> bool:
    if isinstance(dep, (list, tuple)):
        return any(str(item) in removed_task_ids for item in dep)
    if isinstance(dep, dict):
        return any(str(dep.get(key) or "") in removed_task_ids for key in ("source", "target", "from", "to", "depends_on", "task_id"))
    return str(dep) in removed_task_ids


def _replace_unbound_placeholders(sql: str) -> str:
    value = str(sql)
    value = re.sub(r"\$\d+\b", "'repro_value'", value)
    value = re.sub(r"<\s*(?:specific_)?[A-Za-z0-9_]*uuid[A-Za-z0-9_]*\s*>", "'repro_value'", value, flags=re.I)
    value = re.sub(r"<\s*[A-Za-z0-9_]*placeholder[A-Za-z0-9_]*\s*>", "'repro_value'", value, flags=re.I)
    value = re.sub(r"(?<!:):[A-Za-z_][A-Za-z0-9_]*\b", "'repro_value'", value)
    value = re.sub(r"(?<!\w)@[A-Za-z_][A-Za-z0-9_]*\b", "'repro_value'", value)
    value = re.sub(r"=\s*\?", "= 'repro_value'", value)
    value = re.sub(r"\bIN\s*\(\s*\?\s*\)", "IN ('repro_value')", value, flags=re.I)
    return value


def _completed_attempt(attempt_no: int, run_dir: Path, repairs: list[dict[str, Any]]) -> AttemptResult:
    evaluation = _read_json(run_dir / "evaluation_result.json") if (run_dir / "evaluation_result.json").exists() else None
    if evaluation and evaluation.get("success"):
        return AttemptResult(attempt_no, str(run_dir), "success", str(evaluation.get("reason") or "success"), evaluation, repairs)
    return AttemptResult(attempt_no, str(run_dir), "partial", str((evaluation or {}).get("reason") or "completed without successful evaluation"), evaluation, repairs)


def _failure_status(run_dir: Path | None) -> BatchStatus:
    if run_dir and (run_dir / "blueprint.json").exists() and _is_blocked(_read_json(run_dir / "blueprint.json")):
        return "blocked"
    return "failed"


def _is_blocked(blueprint: dict[str, Any]) -> bool:
    if not blueprint:
        return False
    dbms = str((blueprint.get("incident_spec") or {}).get("dbms") or "").lower()
    return (blueprint.get("feasibility") or {}).get("level") == "blocked" or normalize_dbms(dbms) not in supported_execution_dbms()


def _blocked_reason(blueprint: dict[str, Any]) -> str:
    dbms = str((blueprint.get("incident_spec") or {}).get("dbms") or "").lower()
    if dbms and normalize_dbms(dbms) not in supported_execution_dbms():
        return f"No execution adapter for DBMS: {dbms}"
    missing = (blueprint.get("feasibility") or {}).get("missing_capabilities") or []
    return "Missing capabilities: " + "; ".join(map(str, missing)) if missing else str((blueprint.get("feasibility") or {}).get("rationale") or "")


def _missing_capabilities(run_dir: Path | None, blueprint: dict[str, Any] | None = None) -> list[str]:
    if blueprint is None and run_dir and (run_dir / "blueprint.json").exists():
        blueprint = _read_json(run_dir / "blueprint.json")
    if not blueprint:
        return []
    missing = list((blueprint.get("feasibility") or {}).get("missing_capabilities") or [])
    dbms = str((blueprint.get("incident_spec") or {}).get("dbms") or "").lower()
    if dbms and normalize_dbms(dbms) not in supported_execution_dbms():
        missing.append(f"{dbms} execution adapter")
    return list(dict.fromkeys(map(str, missing)))


def _global_changes_have_cleanup(blueprint: dict[str, Any]) -> bool:
    for task in blueprint.get("task_specs") or []:
        text = json.dumps(task.get("actions") or [], ensure_ascii=False).lower()
        if "set global" in text and not task.get("cleanup_actions"):
            return False
    return True


def _latest_run_dir(root: Path) -> Path | None:
    dirs = [path for path in root.iterdir() if path.is_dir() and (path / "state.json").exists()] if root.exists() else []
    return max(dirs, key=lambda path: path.stat().st_mtime) if dirs else None


def _finalize(post_path: Path, category: str, slug: str, attempts: list[AttemptResult]) -> PostResult:
    if _no_progress_planning_timeouts(attempts) >= 2:
        return PostResult(
            str(post_path),
            category,
            slug,
            "abandoned",
            "Stopped after two planning timeouts with no candidate blueprint; no meaningful progress.",
            attempts,
        )
    for status in ("success", "blocked", "abandoned"):
        for attempt in attempts:
            if attempt.status == status:
                return PostResult(str(post_path), category, slug, status, attempt.reason, attempts)
    last = attempts[-1] if attempts else AttemptResult(0, None, "failed", "No attempts were run.")
    return PostResult(str(post_path), category, slug, "abandoned" if attempts else "failed", f"Stopped after {len(attempts)} attempt(s): {last.reason}", attempts)


def _write_summary(output_root: Path, results: list[PostResult], unsupported: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "output_root": str(output_root),
        "generated_at": time.time(),
        "counts": {status: sum(1 for item in results if item.status == status) for status in ("success", "partial", "blocked", "abandoned", "failed")},
        "posts": [item.to_dict() for item in results],
        "unsupported": unsupported,
    }
    _write_json(output_root / "summary.json", summary)
    lines = ["# InputAnalysisAgent Batch Reproduction Summary", "", f"- Output root: `{output_root}`", f"- Posts processed: {len(results)}"]
    for status, count in summary["counts"].items():
        lines.append(f"- {status}: {count}")
    lines += ["", "| Status | Category | Slug | Reason |", "|---|---|---|---|"]
    for item in results:
        reason = item.reason.replace("|", "\\|").replace("\n", " ")[:240]
        lines.append(f"| {item.status} | {item.category} | {item.slug} | {reason} |")
    (output_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    unsupported_lines = ["# Unsupported Tools / Capabilities", ""]
    if unsupported:
        for item in unsupported:
            unsupported_lines.append(f"- `{item['category']}/{item['slug']}` attempt {item['attempt']}: {item['missing_capability']} ({item['reason']})")
    else:
        unsupported_lines.append("No unsupported capabilities recorded yet.")
    (output_root / "unsupported_tools.md").write_text("\n".join(unsupported_lines) + "\n", encoding="utf-8")
    return summary


def _no_progress_planning_timeouts(attempts: list[AttemptResult]) -> int:
    count = 0
    for attempt in attempts:
        if "timed out" not in attempt.reason:
            continue
        if not attempt.run_dir:
            continue
        run_dir = Path(attempt.run_dir)
        state = _read_json(run_dir / "state.json") if (run_dir / "state.json").exists() else {}
        if state.get("phase") == "planning" and not (run_dir / "candidate_blueprint.json").exists():
            count += 1
    return count


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:120] or "post"


def _database_name(category: str, slug: str, attempt: int) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", f"post_retry_{category}_{slug}")[:42].strip("_")
    nonce = int(time.time() * 1000) % 1_000_000
    return f"{base}_r{nonce}_a{attempt}"
