from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from agent_runtime.skill_registry import SkillRegistry


@dataclass
class ProbeProfile:
    database: str
    duration_seconds: int
    sleep_time: float
    thread_count: int
    sql: str = ""


class ProbeWorkloadVerifier:
    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def run_probe(self, profile: ProbeProfile, comparison_mode: str) -> Dict[str, Any]:
        runner = self.skills.get("run_injection_skill")
        result = runner.execute(
            {
                "kind": "workload_profile",
                "database": profile.database,
                "duration_seconds": profile.duration_seconds,
                "sleep_time": profile.sleep_time,
                "thread_count": profile.thread_count,
                "sql": profile.sql,
            }
        )
        result.setdefault("probe_sql", profile.sql)
        result.setdefault("db_evidence", {})
        result["comparison_mode"] = comparison_mode
        return result

    def compare(self, baseline: Dict[str, Any], post: Dict[str, Any]) -> Dict[str, Any]:
        baseline_qps = float(baseline.get("qps", 0.0) or 0.0)
        post_qps = float(post.get("qps", 0.0) or 0.0)
        baseline_p95 = float(baseline.get("p95_latency_ms", 0.0) or 0.0)
        post_p95 = float(post.get("p95_latency_ms", 0.0) or 0.0)
        baseline_failures = int(baseline.get("failed_transactions", 0) or 0)
        post_failures = int(post.get("failed_transactions", 0) or 0)
        qps_ratio = (post_qps / baseline_qps) if baseline_qps else None
        latency_ratio = (post_p95 / baseline_p95) if baseline_p95 else None
        confirmed = (post_qps < baseline_qps) or (post_p95 > baseline_p95) or (post_failures > baseline_failures)
        if confirmed:
            reason = "post TPCC probe shows lower throughput or higher latency under combined anomalies"
        else:
            reason = "combined anomalies executed, but the TPCC probe did not show clear degradation"
        return {
            "baseline": baseline,
            "post": post,
            "delta": {
                "qps_ratio": qps_ratio,
                "p95_ratio": latency_ratio,
                "failure_delta": post_failures - baseline_failures,
            },
            "anomaly_confirmed": confirmed,
            "confirmation_reason": reason,
        }
