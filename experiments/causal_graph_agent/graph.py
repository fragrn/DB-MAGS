from __future__ import annotations

import json
from pathlib import Path
from typing import Any


GRAPH_FILE = Path(__file__).resolve().parent / "anomaly_graph.json"


def load_graph(path: Path | None = None) -> dict[str, Any]:
    graph_path = path or GRAPH_FILE
    return json.loads(graph_path.read_text(encoding="utf-8"))


def get_chain(graph: dict[str, Any], chain_id: str) -> dict[str, Any]:
    chains = graph.get("chains", {})
    if chain_id not in chains:
        available = ", ".join(sorted(chains))
        raise ValueError(f"Unknown chain '{chain_id}'. Available chains: {available}")
    return chains[chain_id]


def validate_chain(graph: dict[str, Any], chain_id: str) -> list[str]:
    chain = get_chain(graph, chain_id)
    nodes = graph.get("nodes", {})
    errors: list[str] = []
    for node_id in chain.get("nodes", []):
        if node_id not in nodes:
            errors.append(f"chain '{chain_id}' references unknown node '{node_id}'")
    return errors


def list_chains(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for chain_id, chain in sorted(graph.get("chains", {}).items()):
        rows.append(
            {
                "id": chain_id,
                "name": chain.get("name", chain_id),
                "db": chain.get("db", "unknown"),
                "status": chain.get("status", "unknown"),
                "nodes": chain.get("nodes", []),
            }
        )
    return rows
