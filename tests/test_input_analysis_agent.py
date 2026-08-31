from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import RuntimeConfig
from InputAnalysisAgent.analyzer import InputAnalysisError, analyze_post
from InputAnalysisAgent.cli import main as cli_main


def valid_payload() -> dict:
    return {
        "post_understanding": {
            "summary": "促销后连接数突增，支付事务出现行锁等待并拖慢查询。",
            "symptoms": ["row lock waits", "slow queries", "qps drop"],
            "suspected_root_causes": ["traffic surge", "hot row update"],
            "key_evidence": ["连接数突增", "支付事务被阻塞"],
            "assumptions": ["使用 MySQL TPC-C payment/new_order 复现"],
            "confidence": 0.82,
        },
        "experiment_environment": {
            "dbms": "mysql",
            "target_database": "dbmags_tpcc_base",
            "target_tables": [
                {
                    "table": "warehouse",
                    "purpose": "支付事务更新热点仓库余额",
                    "columns": [{"name": "w_id", "type": "int", "role": "primary key"}],
                    "data_source": "TPC-C dataset",
                    "generated_data_plan": {
                        "required": False,
                        "row_count": "use existing TPC-C scale",
                        "distribution": "TPC-C default",
                        "key_fields": ["w_id"],
                        "rationale": "已有数据可触发行锁等待",
                    },
                }
            ],
        },
        "background_workloads": [
            {
                "name": "tpcc_payment_mix",
                "inferred_from_post": "帖子提到支付交易",
                "simulation_method": "BenchBase TPCC",
                "transactions_or_queries": ["Payment", "NewOrder"],
                "concurrency": "16 terminals",
                "duration_sec": 120,
                "transaction_mix": {"Payment": 43, "NewOrder": 45},
            }
        ],
        "anomaly_injection": [
            {
                "name": "hot_payment_update",
                "anomaly_type": "lock_contention",
                "mapped_dbmags_anomaly": "hot_update",
                "target": "warehouse row w_id=1",
                "method": "concurrent updates on the same row",
                "parameters": {"holder_concurrency": 1, "waiter_concurrency": 8},
                "duration_sec": 60,
                "safety_notes": ["limit duration and concurrency"],
            }
        ],
        "expected_result": {
            "metrics": [
                {"metric": "Innodb_row_lock_waits", "expected_change": "increase"},
                {"metric": "qps", "expected_change": "decrease"},
            ],
            "query_or_transaction_effects": ["Payment latency rises"],
            "validation_criteria": ["lock waits increase and p95 latency rises"],
        },
        "open_questions": ["真实促销期间峰值连接数是多少？"],
    }


class InputAnalysisAgentTests(unittest.TestCase):
    def test_analyze_post_returns_valid_design(self):
        def fake_llm_generate(**kwargs):
            return {"json_payload": valid_payload(), "text": json.dumps(valid_payload(), ensure_ascii=False)}

        with patch("agent.tools.llm_generate", side_effect=fake_llm_generate):
            design = analyze_post(
                "促销后连接数突增，支付事务被行锁阻塞，慢查询增加，QPS 下降。",
                config=RuntimeConfig(openai_api_key="test-key"),
            )

        self.assertEqual(design.post_understanding["summary"], valid_payload()["post_understanding"]["summary"])
        self.assertEqual(design.anomaly_injection[0]["mapped_dbmags_anomaly"], "hot_update")

    def test_analyze_post_fails_on_invalid_json(self):
        with patch("agent.tools.llm_generate", return_value={"text": "not json"}):
            with self.assertRaisesRegex(InputAnalysisError, "valid JSON"):
                analyze_post("慢查询暴增", config=RuntimeConfig(openai_api_key="test-key"))

    def test_analyze_post_fails_on_missing_required_field(self):
        payload = valid_payload()
        payload.pop("expected_result")
        with patch("agent.tools.llm_generate", return_value={"json_payload": payload}):
            with self.assertRaisesRegex(InputAnalysisError, "missing required fields"):
                analyze_post("慢查询暴增", config=RuntimeConfig(openai_api_key="test-key"))

    def test_cli_accepts_txt_input_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "post.txt"
            output_path = root / "plan.json"
            input_path.write_text("促销后连接数突增，支付事务被行锁阻塞。")

            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
                with patch("agent.tools.llm_generate", return_value={"json_payload": valid_payload()}):
                    rc = cli_main(["--input", str(input_path), "--output", str(output_path)])

            self.assertEqual(rc, 0)
            written = json.loads(output_path.read_text())
            self.assertEqual(written["anomaly_injection"][0]["mapped_dbmags_anomaly"], "hot_update")

    def test_cli_accepts_json_input_and_prints_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "post.json"
            input_path.write_text(json.dumps({
                "dba_description": "CPU 打满后慢查询增多。",
                "metadata": {"source": "forum"},
            }, ensure_ascii=False))
            stdout = io.StringIO()

            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
                with patch("agent.tools.llm_generate", return_value={"json_payload": valid_payload()}):
                    with contextlib.redirect_stdout(stdout):
                        rc = cli_main(["--input", str(input_path)])

            self.assertEqual(rc, 0)
            printed = json.loads(stdout.getvalue())
            self.assertIn("post_understanding", printed)

    def test_cli_returns_nonzero_for_invalid_llm_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "post.txt"
            input_path.write_text("慢查询暴增")
            stderr = io.StringIO()

            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
                with patch("agent.tools.llm_generate", return_value={"text": "not json"}):
                    with contextlib.redirect_stderr(stderr):
                        rc = cli_main(["--input", str(input_path)])

            self.assertEqual(rc, 1)
            self.assertIn("valid JSON", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
