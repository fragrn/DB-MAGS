"""Tests for the optional anomalychains ChainAgent Reflection mode."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from anomalychains import run_cases
from anomalychains.cases import CASE_BY_ID
from db_repro_agent.config import ExperimentProfile
from db_repro_agent.evaluation.edge_evaluator import EdgeEvaluation
from db_repro_agent.evaluation.node_evaluator import NodeEvaluation
from db_repro_agent.models.action import ActionResult, ActionResultStatus
from db_repro_agent.tools.database.base import ToolResult
from db_repro_agent.tools.database.mysql import MySQLConfig


def test_reflection_requires_real_execution(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_cases", "--case", "01_backup_io_redo_qps", "--reflection"])

    with pytest.raises(SystemExit) as error:
        run_cases.main()

    assert error.value.code == 2


def test_default_planning_path_does_not_call_llm(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_cases, "OUTPUT_ROOT", tmp_path / "runs")

    class UnexpectedLLM:
        @classmethod
        def from_config(cls, config):
            raise AssertionError("LLM must not be initialized without --reflection")

    monkeypatch.setattr(run_cases, "ActionGenerator", UnexpectedLLM)
    monkeypatch.setattr(sys, "argv", ["run_cases", "--case", "01_backup_io_redo_qps"])

    assert run_cases.main() == 0


class FakeAdapter:
    def _ok(self, data):
        now = datetime.now(timezone.utc)
        return ToolResult.ok(data, now, now)

    def probe_schema(self, database):
        return self._ok({"database": database, "tables": []})

    def probe_indexes(self, database, table):
        return self._ok([])

    def probe_row_counts(self, database):
        return self._ok({"database": database, "row_counts": {}})

    def probe_runtime(self):
        return self._ok({"status": {"Threads_running": 1}})


class SequenceLLM:
    def __init__(self):
        self.calls = []

    def generate_action(self, context):
        self.calls.append(context)
        return {
            "type": "sql",
            "action_id": f"backup-action-{len(self.calls)}",
            "target_node": "backup",
            "database": "tpcc10_test",
            "sql": "SELECT SLEEP(1)",
            "duration_sec": 2,
        }


class FakePropagationRunner:
    calls = 0

    def __init__(self, **kwargs):
        pass

    def run(self, request, *, observation_provider):
        type(self).calls += 1
        success = type(self).calls > 1
        failed_node = None if success else "redo_log_flush_stall"
        node_results = {
            "redo_log_flush_stall": NodeEvaluation(
                node_id="redo_log_flush_stall",
                hit=success,
                metrics={"innodb_log_waits_delta": 0},
                reason="synthetic",
            )
        }
        graph = SimpleNamespace(
            failed_node=failed_node,
            failed_edge=None if success else "backup->redo_log_flush_stall",
            node_results=node_results,
            edge_results=[
                EdgeEvaluation(
                    source="backup",
                    target="redo_log_flush_stall",
                    satisfied=success,
                    reason="synthetic",
                )
            ],
        )
        now = datetime.now(timezone.utc).isoformat()
        action_result = ActionResult(
            action_id=request.root_action.action_id,
            action_type=request.root_action.type,
            success=True,
            status=ActionResultStatus.SUCCEEDED,
            started_at=now,
            finished_at=now,
            duration_ms=1,
        )
        return SimpleNamespace(
            full_graph_success=success,
            graph_evaluation=graph,
            first_failed_edge=graph.failed_edge,
            root_action_result=action_result,
        )


def test_reflection_mode_retries_with_fake_llm_and_runner(tmp_path, monkeypatch) -> None:
    FakePropagationRunner.calls = 0
    monkeypatch.setattr(run_cases, "PropagationRunner", FakePropagationRunner)
    monkeypatch.setattr(run_cases, "_benchbase_action", lambda *args, **kwargs: None)

    llm = SequenceLLM()
    from db_repro_agent.agent.action_generator import ActionGenerator

    result = run_cases._run_reflection_case(
        CASE_BY_ID["01_backup_io_redo_qps"],
        adapter=FakeAdapter(),
        config=MySQLConfig(database="tpcc10_test"),
        env={},
        profile=ExperimentProfile(),
        output_root=tmp_path / "run",
        action_generator=ActionGenerator(llm),
        max_retry_rounds=1,
    )

    assert result["reflection_enabled"] is True
    assert result["status"] == "experiment_success"
    assert result["attempt_count"] == 2
    assert result["final_status"] == "experiment_success"
    assert len(llm.calls) == 2
    assert llm.calls[0]["active_injection_nodes"] == ["backup"]
    assert llm.calls[0]["observed_downstream_nodes"] == ["redo_log_flush_stall", "qps_drop"]
    assert (tmp_path / "run" / "01_backup_io_redo_qps" / "chain" / "attempt_001" / "reflection.json").exists()
    assert (tmp_path / "run" / "01_backup_io_redo_qps" / "chain" / "attempt_002" / "action_plan.json").exists()
