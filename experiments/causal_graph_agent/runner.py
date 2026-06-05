from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .agent import ExperimentAgent
from .core import ChainRunResult, VerificationResult
from .graph import get_chain, load_graph, validate_chain
from .postgres_adapter import run_dead_tuples_chain
from .verifiers import load_pg_chain_results, pg_summary_to_verifier_results


READY_CHAIN_HANDLERS = {
    "dead_tuples_to_temp_io": "postgres_dead_tuples",
}


class ChainRunner:
    def __init__(self, repo_root: Path, graph_path: Path | None = None) -> None:
        self.repo_root = repo_root
        self.graph = load_graph(graph_path)
        self.agent = ExperimentAgent()

    def run(
        self,
        chain_id: str,
        params: dict[str, Any] | None = None,
        output_root: Path | None = None,
        max_tuning_rounds: int = 0,
        shutdown: bool = False,
    ) -> ChainRunResult:
        errors = validate_chain(self.graph, chain_id)
        if errors:
            raise ValueError("; ".join(errors))
        chain = get_chain(self.graph, chain_id)
        merged_params = dict(chain.get("default_params", {}))
        merged_params.update(params or {})
        if READY_CHAIN_HANDLERS.get(chain_id) == "postgres_dead_tuples":
            merged_params.setdefault("work_mem_profile", "1MB")
            merged_params.setdefault("reset_environment", True)
        run_root = output_root or (self.repo_root / "experiments" / "causal_graph_agent" / "runs")
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = run_root / f"{chain_id}-{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)

        tuning_history: list[dict[str, Any]] = []
        current_params = merged_params
        last_run_params = dict(current_params)
        final_result: ChainRunResult | None = None
        rounds = max(1, max_tuning_rounds + 1)
        for round_index in range(rounds):
            round_dir = output_dir / f"round_{round_index:02d}"
            if chain_id not in READY_CHAIN_HANDLERS:
                final_result = self._planned_result(chain_id, chain, round_dir, current_params, tuning_history)
                last_run_params = dict(current_params)
                break

            final_result = self._run_ready_chain(chain_id, chain, round_dir, current_params, shutdown)
            last_run_params = dict(current_params)
            round_event = {
                "round": round_index,
                "input_params": dict(current_params),
                "effective_work_mem": final_result.raw_summary.get("effective_work_mem"),
                "verdict": {item.node_id: item.hit for item in final_result.verifier_results},
                "complete": final_result.complete,
            }
            if final_result.complete:
                tuning_history.append(round_event)
                break
            failed_node = self._first_failed_node(final_result.verifier_results)
            next_params = self.agent.tune(failed_node, current_params)
            round_event["failed_node"] = failed_node
            round_event["new_params"] = dict(next_params)
            round_event["reason"] = self._tuning_reason(failed_node, current_params, next_params)
            tuning_history.append(round_event)
            current_params = next_params

        assert final_result is not None
        final_result.tuning_history = tuning_history
        self._write_agent_outputs(output_dir, chain_id, chain, last_run_params, final_result)
        return final_result

    def _run_ready_chain(
        self,
        chain_id: str,
        chain: dict[str, Any],
        output_dir: Path,
        params: dict[str, Any],
        shutdown: bool,
    ) -> ChainRunResult:
        run_dead_tuples_chain(self.repo_root, output_dir, params, shutdown=shutdown)
        summary, _samples = load_pg_chain_results(output_dir)
        verifier_results = pg_summary_to_verifier_results(chain.get("nodes", []), summary)
        complete = all(result.hit for result in verifier_results)
        return ChainRunResult(
            chain_id=chain_id,
            complete=complete,
            output_dir=output_dir,
            verifier_results=verifier_results,
            raw_summary=summary,
        )

    def _planned_result(
        self,
        chain_id: str,
        chain: dict[str, Any],
        output_dir: Path,
        params: dict[str, Any],
        tuning_history: list[dict[str, Any]],
    ) -> ChainRunResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        results = [
            VerificationResult(
                node_id=node_id,
                hit=False,
                reason="chain is defined but not executable in the first implementation pass",
                evidence={"status": chain.get("status", "planned"), "params": params},
            )
            for node_id in chain.get("nodes", [])
        ]
        result = ChainRunResult(
            chain_id=chain_id,
            complete=False,
            output_dir=output_dir,
            verifier_results=results,
            raw_summary={"status": chain.get("status", "planned")},
            tuning_history=tuning_history,
        )
        (output_dir / "report.md").write_text(render_report(chain, result), encoding="utf-8")
        return result

    def _first_failed_node(self, results: list[VerificationResult]) -> str | None:
        for result in results:
            if not result.hit:
                return result.node_id
        return None

    def _tuning_reason(self, failed_node: str | None, old_params: dict[str, Any], new_params: dict[str, Any]) -> str:
        if (
            failed_node in {"sort_hash_spill", "temp_io_workfile_write"}
            and old_params.get("work_mem_profile") != new_params.get("work_mem_profile")
        ):
            return "spill_miss_lower_work_mem"
        if old_params == new_params:
            return "no_tuning_available"
        return f"{failed_node or 'unknown'}_miss_tune_params"

    def _write_agent_outputs(
        self,
        output_dir: Path,
        chain_id: str,
        chain: dict[str, Any],
        params: dict[str, Any],
        result: ChainRunResult,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "graph_chain.json").write_text(json.dumps({"chain_id": chain_id, "chain": chain}, indent=2), encoding="utf-8")
        (output_dir / "params.json").write_text(json.dumps(params, indent=2), encoding="utf-8")
        (output_dir / "verifier_results.json").write_text(
            json.dumps([_result_to_dict(item) for item in result.verifier_results], indent=2),
            encoding="utf-8",
        )
        (output_dir / "tuning_history.json").write_text(json.dumps(result.tuning_history, indent=2), encoding="utf-8")
        (output_dir / "report.md").write_text(render_report(chain, result), encoding="utf-8")


def _result_to_dict(result: VerificationResult) -> dict[str, Any]:
    return {
        "node_id": result.node_id,
        "hit": result.hit,
        "reason": result.reason,
        "evidence": result.evidence,
    }


def render_report(chain: dict[str, Any], result: ChainRunResult) -> str:
    lines = [
        "# Causal Graph Agent Report",
        "",
        f"- Chain: `{result.chain_id}`",
        f"- Name: `{chain.get('name', result.chain_id)}`",
        f"- Complete: `{result.complete}`",
        f"- Output dir: `{result.output_dir}`",
        "",
        "## Node Verdicts",
    ]
    for item in result.verifier_results:
        lines.append(f"- `{item.node_id}`: `{'hit' if item.hit else 'miss'}` - {item.reason}")
    lines.extend(["", "## Tuning History"])
    if result.tuning_history:
        for event in result.tuning_history:
            status = "complete" if event.get("complete") else f"failed at `{event.get('failed_node')}`"
            reason = event.get("reason", "no tuning was needed")
            lines.append(
                f"- round `{event['round']}` {status}; "
                f"work_mem=`{event.get('effective_work_mem')}`; reason=`{reason}`"
            )
    else:
        lines.append("- no tuning was needed")
    lines.extend(["", "## Raw Summary", "```json", json.dumps(result.raw_summary, indent=2), "```", ""])
    return "\n".join(lines)
