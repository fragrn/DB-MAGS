from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.evaluator import evaluate_node
from agent.graph import ANOMALY_GRAPH, reachable_paths
from agent.planner import SYSTEM_PROMPT
from agent.types import ExperimentRequest, NodeCategory


class AnomalyGraphExpansionTests(unittest.TestCase):
    def test_new_nodes_exist_with_expected_categories(self):
        injectable = [
            "metadata_lock",
            "table_lock",
            "network_latency",
            "disk_full_or_pressure",
            "deadlock_storm",
            "large_temp_table",
            "redo_log_pressure",
            "connection_storm",
        ]
        for node_id in injectable:
            node = ANOMALY_GRAPH.node(node_id)
            self.assertIsNotNone(node, node_id)
            self.assertEqual(node.category, NodeCategory.INJECTABLE)
            self.assertTrue(node.injectable, node_id)

        for node_id in [
            "metadata_lock_wait",
            "connection_pressure",
            "temp_table_spill",
            "buffer_pool_pressure",
            "redo_log_flush_stall",
            "binlog_flush_stall",
            "deadlock_detected",
            "disk_saturation",
            "network_stall",
        ]:
            node = ANOMALY_GRAPH.node(node_id)
            self.assertIsNotNone(node, node_id)
            self.assertEqual(node.category, NodeCategory.INTERMEDIATE)
            self.assertFalse(node.injectable, node_id)

        for node_id in ["commit_latency_up", "connection_error", "write_throughput_drop"]:
            node = ANOMALY_GRAPH.node(node_id)
            self.assertIsNotNone(node, node_id)
            self.assertEqual(node.category, NodeCategory.SYMPTOM)
            self.assertFalse(node.injectable, node_id)

    def test_edges_have_existing_endpoints_and_no_metric_name_nodes(self):
        metric_names = {
            "rows_examined_ratio",
            "qps_ratio",
            "tps_ratio",
            "Created_tmp_disk_tables_delta",
        }
        for edge in ANOMALY_GRAPH.edges:
            self.assertIn(edge.src, ANOMALY_GRAPH.nodes, edge)
            self.assertIn(edge.dst, ANOMALY_GRAPH.nodes, edge)
            self.assertNotIn(edge.src, metric_names)
            self.assertNotIn(edge.dst, metric_names)

    def test_reachable_paths_cover_new_representative_chains(self):
        expected = [
            ("metadata_lock", "qps_drop", ["metadata_lock", "metadata_lock_wait", "slow_query", "qps_drop"]),
            ("deadlock_storm", "write_throughput_drop", ["deadlock_storm", "deadlock_detected", "write_throughput_drop"]),
            ("connection_storm", "qps_drop", ["connection_storm", "connection_pressure", "timeout", "qps_drop"]),
            ("network_latency", "qps_drop", ["network_latency", "network_stall", "timeout", "qps_drop"]),
            ("large_temp_table", "qps_drop", ["large_temp_table", "temp_table_spill", "slow_query", "qps_drop"]),
            ("redo_log_pressure", "write_throughput_drop", ["redo_log_pressure", "redo_log_flush_stall", "commit_latency_up", "write_throughput_drop"]),
            ("disk_full_or_pressure", "qps_drop", ["disk_full_or_pressure", "disk_saturation", "slow_query", "qps_drop"]),
        ]
        for source, target, path in expected:
            self.assertIn(path, reachable_paths(source, target), path)

    def test_planner_prompt_mentions_new_injectables_and_strategies(self):
        for text in [
            "metadata_lock",
            "table_lock",
            "network_latency",
            "disk_full_or_pressure",
            "deadlock_storm",
            "large_temp_table",
            "redo_log_pressure",
            "connection_storm",
            "Do not attempt to fill the system disk",
            "network drop",
            "expected_error_codes: [1213]",
        ]:
            self.assertIn(text, SYSTEM_PROMPT)

    def test_new_evidence_rules_can_hit_from_constructed_metrics(self):
        cases = [
            ("connection_pressure", {"Threads_connected": 10}, {"Threads_connected": 20, "active_sessions_delta": 8}),
            ("temp_table_spill", {}, {"Created_tmp_disk_tables_delta": 2}),
            ("deadlock_detected", {}, {"deadlock_delta": 1}),
            ("commit_latency_up", {"write_latency_ratio": 1.0, "tps_ratio": 1.0}, {"write_latency_ratio": 2.0, "tps_ratio": 0.6}),
            ("write_throughput_drop", {"tps_ratio": 1.0}, {"tps_ratio": 0.6}),
            ("connection_error", {}, {"connection_error_delta": 1}),
            ("metadata_lock_wait", {}, {"metadata_lock_evidence": "Waiting for table metadata lock"}),
        ]
        for node_id, baseline, after in cases:
            result = evaluate_node(node_id, baseline, after)
            self.assertTrue(result.hit, f"{node_id}: {result.details}")

    def test_new_request_json_files_parse_and_injected_nodes_are_injectable(self):
        root = Path("experiment_runs")
        for name in [
            "request_metadata_lock_chain.json",
            "request_table_lock_chain.json",
            "request_deadlock_storm_chain.json",
            "request_connection_storm_chain.json",
            "request_network_latency_chain.json",
            "request_large_temp_table_chain.json",
            "request_redo_log_pressure_chain.json",
            "request_backup_redo_stall_chain.json",
            "request_disk_pressure_chain.json",
            "request_memory_buffer_pool_chain.json",
        ]:
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            request = ExperimentRequest.from_dict(payload)
            self.assertGreaterEqual(len(request.target_path), 2, name)
            for node_id in request.target_path:
                self.assertIn(node_id, ANOMALY_GRAPH.nodes, name)
            for node_id in request.injected_nodes:
                node = ANOMALY_GRAPH.node(node_id)
                self.assertIsNotNone(node, name)
                self.assertTrue(node.injectable, name)


if __name__ == "__main__":
    unittest.main()
