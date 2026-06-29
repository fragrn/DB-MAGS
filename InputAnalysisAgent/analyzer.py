"""LLM-backed analyzer for DBA forum incident descriptions."""

from __future__ import annotations

import json
from typing import Any

from agent import tools as tool_registry
from agent.config import RuntimeConfig

from InputAnalysisAgent.types import AnalysisRequest, ReproductionDesign


class InputAnalysisError(RuntimeError):
    """Raised when the input analysis agent cannot produce a valid design."""


SYSTEM_PROMPT = """You are InputAnalysisAgent for DB-MAGS.

Your job is to read a DBA forum post describing a database incident and produce a JSON-only
database anomaly reproduction design document. Do not execute anything. Do not claim that
an experiment has already been run.

You may make reasonable assumptions for a reproducible experiment, but every assumption must
be explicitly marked in the output. If the post lacks key details, keep the design actionable
and add concise questions to open_questions.

Return exactly one JSON object with these required top-level fields:
{
  "post_understanding": {
    "summary": "brief understanding of the incident",
    "symptoms": ["slow query", "lock wait", "..."],
    "suspected_root_causes": ["traffic surge", "..."],
    "key_evidence": ["quoted or paraphrased clues from the post"],
    "assumptions": ["assumptions made for reproduction"],
    "confidence": 0.0
  },
  "experiment_environment": {
    "dbms": "mysql/postgresql/unknown",
    "target_database": "database name or proposed synthetic database",
    "target_tables": [
      {
        "table": "table name",
        "purpose": "why this table is used",
        "columns": [{"name": "column", "type": "type", "role": "primary key/filter/hot key/..."}],
        "data_source": "existing dataset / DBA-provided dump / generated data",
        "generated_data_plan": {
          "required": true,
          "row_count": "size",
          "distribution": "data distribution",
          "key_fields": ["fields"],
          "rationale": "why this data is needed"
        }
      }
    ]
  },
  "background_workloads": [
    {
      "name": "workload name",
      "inferred_from_post": "why this workload is believed to exist",
      "simulation_method": "SQL script / BenchBase / custom client",
      "transactions_or_queries": ["statements or transaction descriptions"],
      "concurrency": "threads/connections",
      "duration_sec": 0,
      "transaction_mix": {"operation": "percentage or qualitative ratio"}
    }
  ],
  "anomaly_injection": [
    {
      "name": "injection task name",
      "anomaly_type": "traffic_surge / lock_contention / slow_sql / resource_cpu / ...",
      "mapped_dbmags_anomaly": "optional DB-MAGS graph node such as traffic_surge, hot_update, resource_cpu",
      "target": "database/table/row/resource",
      "method": "how to inject the anomaly",
      "parameters": {"key": "value"},
      "duration_sec": 0,
      "safety_notes": ["bounded safety notes"]
    }
  ],
  "expected_result": {
    "metrics": [{"metric": "metric name", "expected_change": "increase/decrease/threshold"}],
    "query_or_transaction_effects": ["expected user-visible DB behavior"],
    "validation_criteria": ["how to decide the reproduction matches the post"]
  },
  "open_questions": ["questions for the DBA when details are missing"]
}

The four core design sections must answer:
1. Which database and tables to run on, where data comes from, and what to generate if needed.
2. Which workloads are currently running and how to simulate them.
3. Which anomaly injection tasks to run.
4. What results are expected.

Only return JSON. No markdown, no prose outside the JSON object.
"""


def analyze_post(
    dba_description: str,
    *,
    metadata: dict[str, Any] | None = None,
    config: RuntimeConfig | None = None,
) -> ReproductionDesign:
    """Analyze a DBA incident post and return a validated reproduction design."""
    request = AnalysisRequest(dba_description=dba_description, metadata=metadata or {})
    config = config or RuntimeConfig.from_env()
    if not config.openai_api_key:
        raise InputAnalysisError("Input analysis requires OPENAI_API_KEY")

    response = tool_registry.llm_generate(
        config=config,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(request),
        temperature=config.planner_temperature,
        json_mode=True,
    )
    if response.get("error"):
        raise InputAnalysisError(f"LLM request failed: {response['error']}")

    payload = response.get("json_payload")
    if payload is None:
        text = str(response.get("text") or "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InputAnalysisError(f"LLM response did not contain valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InputAnalysisError("LLM response JSON must be an object")

    try:
        return ReproductionDesign.from_dict(payload)
    except ValueError as exc:
        raise InputAnalysisError(str(exc)) from exc


def _build_user_prompt(request: AnalysisRequest) -> str:
    return (
        "## DBA Forum Post\n"
        f"{request.dba_description}\n\n"
        "## Metadata\n"
        f"{json.dumps(request.metadata, ensure_ascii=False, indent=2)}\n"
    )
