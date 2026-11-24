"""M9 manual propagation runner tests using synthetic observations."""

from __future__ import annotations

import time

from db_repro_agent.execution.command_executor import _iso
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.models.action import ActionResult, BenchBaseAction, SQLAction
from db_repro_agent.models.anomaly_graph import AnomalyEdge, AnomalyGraph, AnomalyNode
from db_repro_agent.reproduction.injection_actions import (
    build_backup_action,
    build_missing_index_action,
    build_redo_log_pressure_action,
)
from db_repro_agent.evaluation.node_evaluator import NodeObservation
from db_repro_agent.evaluation.node_evaluator import NodeEvaluator
from db_repro_agent.reproduction.propagation_runner import (
    PropagationReproductionRequest,
    PropagationRunner,
)


class CountingProvider:
    def __init__(self) -> None:
        self.count = 0

    def collect(self) -> dict[str, float]:
        self.count += 1
        return {"qps": float(self.count)}


class FakeBenchBase:
    def __init__(self) -> None:
        self.started = 0
        self.cleaned = 0

    def start(self, action):
        self.started += 1
        return result(action.action_id, action.type, "started")

    def cleanup(self, action):
        self.cleaned += 1
        return result(action.action_id, action.type, "cleaned")

    def is_alive(self):
        return self.started > self.cleaned


class FakeDispatcher:
    def __init__(self) -> None:
        self.benchbase = FakeBenchBase()
        self.executed: list[str] = []
        self.cleaned: list[str] = []

    def validate(self, action) -> None:
        return None

    def start(self, action):
        self.executed.append(f"start:{action.action_id}")
        return result(action.action_id, action.type, "started")

    def execute(self, action):
        self.executed.append(f"execute:{action.action_id}")
        return result(action.action_id, action.type, "succeeded")

    def cleanup(self, action):
        self.cleaned.append(action.action_id)
        return result(action.action_id, action.type, "cleaned")


def result(action_id: str, action_type: str, status: str) -> ActionResult:
    now = _iso(time.time())
    return ActionResult(
        action_id=action_id,
        action_type=action_type,
        success=True,
        status=status,
        started_at=now,
        finished_at=now,
        duration_ms=1,
    )


def graph() -> AnomalyGraph:
    return AnomalyGraph(
        graph_id="manual-chain",
        root_nodes=["traffic_surge"],
        nodes=[
            AnomalyNode(node_id="traffic_surge"),
            AnomalyNode(node_id="connections_up"),
            AnomalyNode(node_id="row_lock"),
        ],
        edges=[
            AnomalyEdge(source="traffic_surge", target="connections_up", mechanism="connections", provenance="human", confidence=1),
            AnomalyEdge(source="connections_up", target="row_lock", mechanism="locks", provenance="human", confidence=1),
        ],
    )


def request() -> PropagationReproductionRequest:
    return PropagationReproductionRequest(
        incident_id="propagation-m9",
        graph=graph(),
        root_action=SQLAction(
            action_id="traffic-root",
            target_node="traffic_surge",
            database="tpcc10_test",
            sql="SELECT 1",
        ),
        baseline_seconds=0.03,
        observe_seconds=0.04,
        recovery_seconds=0.01,
    )


def make_runner() -> tuple[PropagationRunner, FakeDispatcher, CountingProvider]:
    dispatcher = FakeDispatcher()
    provider = CountingProvider()
    sampler = MetricsSampler(MetricsCollector({"synthetic": provider}), interval_seconds=0.005)
    return PropagationRunner(dispatcher=dispatcher, sampler=sampler), dispatcher, provider


def observations(all_hit: bool = True) -> list[NodeObservation]:
    return [
        NodeObservation(node_id="traffic_surge", hit=True, timestamp_sec=1),
        NodeObservation(node_id="connections_up", hit=all_hit, timestamp_sec=3),
        NodeObservation(node_id="row_lock", hit=all_hit, timestamp_sec=6),
    ]


def test_root_only_is_executed_and_full_graph_is_evaluated() -> None:
    runner, dispatcher, provider = make_runner()
    result = runner.run(request(), observations=observations())

    assert result.status.value == "experiment_success"
    assert result.full_graph_success is True
    assert result.graph_evaluation is not None
    assert result.graph_evaluation.all_nodes_hit is True
    assert result.graph_evaluation.all_edges_satisfied is True
    assert dispatcher.executed == ["start:traffic-root", "execute:traffic-root"]
    assert dispatcher.cleaned == ["traffic-root"]
    assert provider.count >= 3
    assert result.metrics_sample_count >= 3
    assert result.node_first_trigger_times == {
        "traffic_surge": 1.0,
        "connections_up": 3.0,
        "row_lock": 6.0,
    }


def test_background_workload_is_started_and_cleaned() -> None:
    runner, dispatcher, _ = make_runner()
    background = BenchBaseAction(
        action_id="tpcc-background",
        target_node="traffic_surge",
        benchmark="tpcc",
        database="tpcc10_test",
        terminals=1,
        duration_sec=60,
        config_path="fixture.xml",
        jar_path="benchbase.jar",
        results_dir="results",
    )
    req = request().model_copy(update={"background_workload": background})
    result = runner.run(req, observations=observations())
    assert result.cleanup_success is True
    assert result.background_alive_before_cleanup is True
    assert result.full_graph_success is True
    assert dispatcher.benchbase.started == 1
    assert dispatcher.benchbase.cleaned == 1


def test_first_failed_edge_is_reported_when_downstream_node_misses() -> None:
    runner, _, _ = make_runner()
    result = runner.run(request(), observations=observations(all_hit=False))

    assert result.full_graph_success is False
    assert result.status.value == "experiment_miss"
    assert result.first_failed_edge == "traffic_surge->connections_up"
    assert result.graph_evaluation.failed_node == "connections_up"


def test_observation_provider_receives_continuous_timeline() -> None:
    runner, _, provider = make_runner()
    seen = []

    def observe(timeline):
        seen.append(len(timeline.samples))
        return observations()

    result = runner.run(request(), observation_provider=observe)
    assert result.full_graph_success is True
    assert seen and seen[0] >= 1
    assert provider.count >= 3


def test_repeat_propagation_five_times() -> None:
    outcomes = []
    for index in range(5):
        runner, _, _ = make_runner()
        result = runner.run(request().model_copy(update={"incident_id": f"propagation-{index}"}), observations=observations())
        outcomes.append(result.full_graph_success)
    assert outcomes == [True] * 5


def test_downstream_node_rules_are_metric_driven() -> None:
    evaluator = NodeEvaluator()
    cases = [
        ("metadata_lock_wait", {"metadata_lock_wait_count": 1}, True),
        ("poor_plan", {"rows_examined_ratio": 1.1}, True),
        ("poor_plan", {"explain_access_type": "ALL"}, True),
        ("improper_sql", {"rows_examined_ratio": 3.1}, True),
        ("temp_table_spill", {"created_tmp_disk_tables_delta": 1}, True),
        ("sort_hash_spill", {"sort_merge_passes_delta": 1}, True),
        ("sort_hash_spill", {"sort_merge_passes_ratio": 2}, False),
        ("resource_bottleneck_cpu", {"cpu_usage_ratio": 1.2}, True),
        ("disk_saturation", {"disk_util": 0.8}, True),
        (
            "buffer_pool_pressure",
            {
                "buffer_pool_miss_ratio": 0.10,
                "buffer_pool_miss_ratio_valid": True,
                "buffer_pool_baseline_miss_ratio": 0.05,
                "buffer_pool_baseline_miss_ratio_valid": True,
                "buffer_pool_physical_reads_per_sec": 4.0,
                "buffer_pool_physical_reads_valid": True,
                "buffer_pool_baseline_physical_reads_per_sec": 2.0,
                "buffer_pool_baseline_physical_reads_valid": True,
            },
            True,
        ),
        ("redo_log_flush_stall", {"innodb_log_waits_delta": 1}, True),
        ("write_throughput_drop", {"tps_ratio": 0.69}, True),
        ("connection_pressure", {"threads_connected_delta": 1}, True),
        ("timeout", {"timeout_error_count": 1}, True),
        ("deadlock_detected", {"deadlock_delta": 1}, True),
        ("network_stall", {"network_error_delta": 1}, True),
        ("poor_plan", {"rows_examined_ratio": 1.0}, False),
        ("write_throughput_drop", {"tps_ratio": 0.7}, False),
    ]
    for node_id, metrics, expected in cases:
        evaluation = evaluator.evaluate(
            AnomalyNode(node_id=node_id),
            NodeObservation(node_id=node_id, metrics=metrics),
        )
        assert evaluation.hit is expected, (node_id, metrics, evaluation.reason)


def test_m9_does_not_require_root_observation() -> None:
    runner, _, _ = make_runner()
    result = runner.run(
        request(),
        observations=[
            NodeObservation(node_id="connections_up", hit=True, timestamp_sec=3),
            NodeObservation(node_id="row_lock", hit=True, timestamp_sec=6),
        ],
    )
    assert result.full_graph_success is True
    assert result.graph_evaluation is not None
    assert result.graph_evaluation.node_results["traffic_surge"].evaluated is False


def test_backup_injection_is_a_bounded_sql_action() -> None:
    action = build_backup_action("backup-1", "tpcc10_test")
    assert action.target_node == "backup"
    assert action.sql == "CREATE TABLE dbmags_backup_shadow AS SELECT * FROM stock"
    assert action.concurrency == 1


def test_missing_index_injection_queries_without_schema_mutation() -> None:
    action = build_missing_index_action("missing-index-1", "tpcc10_test")
    assert action.target_node == "missing_index"
    assert "SELECT * FROM stock" in action.sql
    assert "LIKE '%DBMAGS-MISSING-INDEX%'" in action.sql
    assert "ORDER BY s_data" in action.sql
    assert not any(keyword in action.sql.upper() for keyword in ("CREATE INDEX", "DROP INDEX", "ALTER TABLE"))


def test_redo_log_pressure_is_bounded_concurrent_write_sql() -> None:
    action = build_redo_log_pressure_action("redo-1", "tpcc10_test", concurrency=4)
    assert action.target_node == "redo_log_pressure"
    assert action.execution_mode == "concurrent"
    assert action.concurrency == 4
    assert action.sql.startswith("UPDATE stock SET s_quantity = MOD(s_quantity + 1, 100)")
    assert action.timeout_sec > action.duration_sec
