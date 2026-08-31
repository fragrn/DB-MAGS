"""
Markdown report generator for experiment runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.types import RunResult, to_jsonable


def write_report(result: RunResult, path: Path) -> None:
    """
    Write a structured Markdown report for a completed experiment run.
    """
    ev = result.evaluation
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    # Header
    lines.append(f"# DBMAGS Experiment Report — {result.run_id}")
    lines.append("")
    lines.append(f"**Status**: {'SUCCESS' if ev.success else 'FAILED'} | "
                 f"Rounds: {result.rounds} | "
                 f"Final Score: {ev.final_score:.3f}")
    lines.append("")

    # 1. Experiment Target
    lines.append("## 1. Experiment Target")
    lines.append(f"- **Anomaly**: {result.request.target_anomaly}")
    lines.append(f"- **Database**: {result.request.target_database}")
    if result.request.dba_description:
        lines.append(f"- **DBA Description**: {result.request.dba_description}")
    if result.request.target_path:
        lines.append(f"- **Target Path**: `{' -> '.join(result.request.target_path)}`")
    if result.request.injected_nodes:
        lines.append(f"- **Injected Nodes**: `{' -> '.join(result.request.injected_nodes)}`")
    lines.append(f"- **Max Duration**: {result.request.max_duration_sec}s")
    lines.append(f"- **Risk Level**: {result.request.risk_level}")
    lines.append("")

    # 2. Environment
    lines.append("## 2. Environment")
    snap = result.snapshot
    if snap and snap.schema:
        lines.append(f"- **DB Version**: {snap.db_version}")
        lines.append(f"- **Tables**: {len(snap.schema.tables)}")
    if snap and snap.db_metrics:
        dbm = snap.db_metrics
        lines.append(
            f"- **Threads**: connected={dbm.get('Threads_connected','?')}, "
            f"running={dbm.get('Threads_running','?')}"
        )
        lines.append(f"- **Slow Queries**: {dbm.get('Slow_queries','?')}")
        lines.append(f"- **Max Connections**: {dbm.get('max_connections', snap.max_connections)}")
    lines.append("")

    # 3. Propagation Chain
    pr = ev.path_result
    if pr:
        lines.append("## 3. Anomaly Propagation Chain")
        lines.append(f"**Target Path**: `{' -> '.join(pr.target_path)}`")
        lines.append(f"**Path Hit**: {pr.path_hit}")
        lines.append(f"**Node Hit Ratio**: {pr.node_hit_ratio:.1%}")
        if pr.broken_edge:
            lines.append(f"**Broken Edge**: `{pr.broken_edge[0]} -> {pr.broken_edge[1]}`")
        if pr.failure_stage:
            lines.append(f"**Failure Stage**: {pr.failure_stage}")
        lines.append("")
        lines.append("### Node Results")
        lines.append("| Node | Hit | Confidence | Evidence |")
        lines.append("|------|-----|------------|----------|")
        for nid, nr in pr.node_results.items():
            hit = "YES" if nr.hit else "NO"
            conf = f"{nr.confidence:.2f}"
            details = (nr.details or "")[:60].replace("|", "\\|")
            lines.append(f"| `{nid}` | {hit} | {conf} | {details} |")
        lines.append("")
    else:
        lines.append("## 3. Propagation Chain")
        lines.append("*No path evaluation available.*")
        lines.append("")

    # 4. Task DAG
    lines.append("## 4. Task DAG")
    if result.dag and result.dag.tasks:
        lines.append("| Task ID | Type | Risk | Actions |")
        lines.append("|---------|------|------|---------|")
        for tid, task in result.dag.tasks.items():
            task_type = task.task_type if hasattr(task, "task_type") else task.get("task_type", "?")
            risk = task.risk_assessment if hasattr(task, "risk_assessment") else task.get("risk_assessment", "?")
            n_actions = len(task.actions) if hasattr(task, "actions") else len(task.get("actions", []))
            lines.append(f"| `{tid}` | {task_type} | {risk} | {n_actions} |")
        if result.dag.edges:
            lines.append("")
            lines.append("**Edges** (dependency order):")
            for e in result.dag.edges:
                src = e.source if hasattr(e, "source") else e.get("source", "?")
                dst = e.target if hasattr(e, "target") else e.get("target", "?")
                lines.append(f"- `{src}` -> `{dst}`")
        lines.append("")
    else:
        lines.append("## 4. Task DAG")
        lines.append("*No tasks in DAG.*")
        lines.append("")

    # 5. Execution Trace
    lines.append("## 5. Execution Trace")
    if result.execution_trace:
        trace = result.execution_trace
        cleanup_status = _get(trace, "cleanup_status", "unknown")
        cleanup_errors = _get(trace, "cleanup_errors", []) or []
        tasks = _get(trace, "tasks", {}) or {}
        lines.append(f"**Cleanup Status**: {cleanup_status}")
        lines.append("")
        lines.append("| Task | Status | Start | End |")
        lines.append("|------|--------|-------|-----|")
        for tid, tr in tasks.items():
            status = _get(tr, "status", "?")
            start = str(_get(tr, "start_time", "-") or "-")[:19]
            end = str(_get(tr, "end_time", "-") or "-")[:19]
            lines.append(f"| `{tid}` | {status} | {start} | {end} |")
        if cleanup_errors:
            lines.append("")
            lines.append("**Cleanup Errors**:")
            for err in cleanup_errors:
                lines.append(f"- {err}")
    else:
        lines.append("*No execution trace available.*")
    lines.append("")

    # 6. Metrics Change
    lines.append("## 6. Metrics Change")
    if ev.baseline_metrics and ev.after_metrics:
        lines.append("| Metric | Baseline | After | Ratio |")
        lines.append("|--------|----------|-------|-------|")
        for section in ("db_metrics", "workload", "os_metrics"):
            base_section = ev.baseline_metrics.get(section, {})
            after_section = ev.after_metrics.get(section, {})
            if not isinstance(base_section, dict):
                continue
            for key in list(base_section.keys())[:10]:
                bval = _fmt(base_section.get(key))
                aval = _fmt(after_section.get(key))
                ratio = _ratio_str(base_section.get(key), after_section.get(key))
                lines.append(f"| {section}.{key} | {bval} | {aval} | {ratio} |")
    lines.append("")

    # 7. Safety
    lines.append("## 7. Safety")
    lines.append(f"**Approved**: {ev.safety_violations == [] if ev.safety_violations else 'See below'}")
    if ev.safety_violations:
        for v in ev.safety_violations:
            lines.append(f"- {v}")
    else:
        lines.append("No safety violations detected.")
    lines.append("")

    # 8. Evaluation Summary
    lines.append("## 8. Evaluation Summary")
    lines.append(f"| Score Component | Value |")
    lines.append(f"|-----------------|-------|")
    lines.append(f"| Final Score | {ev.final_score:.3f} |")
    lines.append(f"| Performance | {ev.performance_score:.3f} |")
    lines.append(f"| Target Anomaly | {ev.target_anomaly_score:.3f} |")
    lines.append(f"| Causal Order | {ev.causal_order_score:.3f} |")
    lines.append(f"| Stability | {ev.stability_score:.3f} |")
    if ev.reason:
        lines.append(f"| Reason | {ev.reason} |")
    lines.append("")

    # 9. Reflection
    if result.reflection:
        lines.append("## 9. Reflection")
        ref = result.reflection
        lines.append(f"**Failure Reason**: {ref.failure_reason}")
        if ref.suggested_changes:
            lines.append("")
            lines.append("**Suggested Changes**:")
            for s in ref.suggested_changes:
                lines.append(f"- {s}")
        if ref.task_parameter_updates:
            lines.append("")
            lines.append("**Parameter Updates**:")
            for node_id, params in ref.task_parameter_updates.items():
                lines.append(f"- `{node_id}`: {json.dumps(params)}")
        if ref.risk_warning:
            lines.append("")
            lines.append(f"**Risk Warning**: {ref.risk_warning}")
        lines.append("")

    comparison = _read_json(Path(result.output_dir) / "reflection_comparison.json")
    if comparison and comparison.get("available"):
        lines.append("## Reflection Comparison")
        lines.append(f"- **Score Delta**: {_fmt(comparison.get('score_delta'))}")
        qps = ((comparison.get("qps") or {}).get("injection") or {})
        lines.append(
            f"- **Injection QPS Avg**: {_fmt(qps.get('before_avg'))} -> "
            f"{_fmt(qps.get('after_avg'))} (delta {_fmt(qps.get('delta'))})"
        )
        latency = (((comparison.get("query_latency") or {}).get("injection") or {}).get("overall") or {})
        before_latency = latency.get("before") or {}
        after_latency = latency.get("after") or {}
        lines.append(
            f"- **Injection Query Latency avg/p95/max ms**: "
            f"{_fmt(before_latency.get('avg_latency_ms'))}/"
            f"{_fmt(before_latency.get('p95_latency_ms'))}/"
            f"{_fmt(before_latency.get('max_latency_ms'))} -> "
            f"{_fmt(after_latency.get('avg_latency_ms'))}/"
            f"{_fmt(after_latency.get('p95_latency_ms'))}/"
            f"{_fmt(after_latency.get('max_latency_ms'))}"
        )
        node_changes = comparison.get("node_hit_changes") or {}
        if node_changes:
            lines.append(f"- **Node Hit Changes**: `{json.dumps(node_changes, ensure_ascii=False)}`")
        lines.append("")

    # 10. Cleanup
    lines.append("## 10. Cleanup")
    if result.execution_trace:
        cleanup_status = _get(result.execution_trace, "cleanup_status", "unknown")
        cleanup_errors = _get(result.execution_trace, "cleanup_errors", []) or []
        lines.append(f"Status: {cleanup_status}")
        if cleanup_errors:
            lines.append("Errors:")
            for e in cleanup_errors:
                lines.append(f"  - {e}")
    lines.append("")

    # 11. Background Workload
    if result.workload_trace:
        trace = result.workload_trace
        cfg = _get(trace, "config", {}) or {}
        status = _get(trace, "status", {}) or {}
        samples = _get(trace, "samples", []) or []
        lines.append("## 11. Background Workload")
        lines.append(f"- **Runner**: {cfg.get('runner', '?')}")
        lines.append(f"- **Benchmark**: {cfg.get('benchmark', cfg.get('type', '?'))}")
        lines.append(f"- **Database**: {cfg.get('database', '?')}")
        lines.append(f"- **PID**: {status.get('pid', '?')}")
        lines.append(f"- **Running at End**: {status.get('running', False)}")
        lines.append(f"- **Exit Code**: {status.get('exit_code', '?')}")
        phase_counts: dict[str, int] = {}
        for sample in samples:
            phase = str(sample.get("phase", "unknown"))
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if phase_counts:
            lines.append("")
            lines.append("| Phase | Samples |")
            lines.append("|-------|---------|")
            for phase, count in sorted(phase_counts.items()):
                lines.append(f"| {phase} | {count} |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt(v: Any) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)[:20]


def _ratio_str(b: Any, a: Any) -> str:
    try:
        bf = float(b)
        af = float(a)
        if bf == 0:
            return "inf" if af > 0 else "1.00"
        return f"{af / bf:.2f}"
    except (TypeError, ValueError):
        return "-"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
