#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.causal_graph_agent.agent import ExperimentAgent
from experiments.causal_graph_agent.config import LLMConfig, MySQLConfig, load_env_file
from experiments.causal_graph_agent.db_connections import check_mysql_connection
from experiments.causal_graph_agent.graph import list_chains, load_graph
from experiments.causal_graph_agent.llm_client import OpenAICompatibleClient
from experiments.causal_graph_agent.runner import ChainRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run constrained causal anomaly graph experiments.")
    parser.add_argument("--chain", default=None, help="Chain id from anomaly_graph.json.")
    parser.add_argument("--goal", default=None, help="Natural-language goal used by the constrained strategy layer.")
    parser.add_argument("--db", default=None, help="Reserved for reporting/filtering; chain definitions own DB routing.")
    parser.add_argument("--max-tuning-rounds", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--list-chains", action="store_true")
    parser.add_argument("--show-config", action="store_true", help="Print sanitized .env-derived config.")
    parser.add_argument("--check-db", action="store_true", help="Check MySQL connectivity from .env.")
    parser.add_argument("--check-llm", action="store_true", help="Check OpenAI-compatible LLM API connectivity from .env.")
    parser.add_argument("--baseline-runs", type=int)
    parser.add_argument("--inject-batch-size", type=int)
    parser.add_argument("--delete-ratio", type=float)
    parser.add_argument("--query-runs", type=int)
    parser.add_argument("--sample-interval-sec", type=float)
    parser.add_argument("--work-mem-profile", choices=["512kB", "1MB", "2MB", "4MB"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    load_env_file(repo_root / ".env")
    graph = load_graph()
    if args.show_config:
        print(json.dumps({"llm": LLMConfig.from_env().safe_dict(), "mysql": MySQLConfig.from_env().safe_dict()}, indent=2))
        return 0
    if args.check_db:
        check = check_mysql_connection()
        print(json.dumps({"mysql": {"ok": check.ok, "details": check.details}}, indent=2))
        return 0 if check.ok else 2
    if args.check_llm:
        client = OpenAICompatibleClient()
        try:
            result = client.ping()
            print(json.dumps({"llm": {"ok": True, "details": result}}, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"llm": {"ok": False, "error": str(exc)}}, indent=2))
            return 2
    if args.list_chains:
        print(json.dumps(list_chains(graph), indent=2))
        return 0

    agent = ExperimentAgent()
    chain_id = agent.select_chain(args.goal, graph, args.chain)
    params = {
        key: value
        for key, value in {
            "baseline_runs": args.baseline_runs,
            "inject_batch_size": args.inject_batch_size,
            "delete_ratio": args.delete_ratio,
            "query_runs": args.query_runs,
            "sample_interval_sec": args.sample_interval_sec,
            "work_mem_profile": args.work_mem_profile,
        }.items()
        if value is not None
    }
    runner = ChainRunner(repo_root)
    result = runner.run(
        chain_id,
        params=params,
        output_root=args.output_dir,
        max_tuning_rounds=args.max_tuning_rounds,
        shutdown=args.shutdown,
    )
    print(f"chain={result.chain_id}")
    print(f"complete={result.complete}")
    print(f"output_dir={result.output_dir}")
    for verifier_result in result.verifier_results:
        status = "hit" if verifier_result.hit else "miss"
        print(f"{verifier_result.node_id}: {status} - {verifier_result.reason}")
    return 0 if result.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
