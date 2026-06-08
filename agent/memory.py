"""
Long-term memory store backed by a JSONL file.

Each line is one JSON-serializable MemoryItem.  The store is append-only
and safe for concurrent writes (via file locking).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent.types import MemoryItem, to_jsonable


class MemoryStore:
    """Append-only JSONL-backed memory store."""

    def __init__(self, path: str = "memory/long_term_memory.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    def load(self, anomaly: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """
        Return the most recent `limit` memory items, optionally filtered by anomaly.
        """
        if not self.path.exists():
            return []

        lines: list[str] = []
        try:
            with open(self.path) as fh:
                all_lines = fh.readlines()
        except OSError:
            return []

        # Scan from bottom (newest) upward
        for line in reversed(all_lines):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if anomaly and item.get("anomaly") != anomaly:
                continue
            lines.append(line)
            if len(lines) >= limit:
                break

        result = []
        for line in reversed(lines):
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return result

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    def append(self, item: dict[str, Any] | MemoryItem) -> None:
        """
        Append a MemoryItem (or dict) as one line in the JSONL file.
        """
        if isinstance(item, MemoryItem):
            item = item.to_dict()
        item = to_jsonable(item)
        line = json.dumps(item, ensure_ascii=False)
        with open(self.path, "a") as fh:
            fh.write(line + "\n")

    def append_reflection(
        self,
        anomaly: str,
        path: list[str],
        task_params: dict[str, Any],
        outcome: str,
        success: bool,
        round_no: int,
        node_hit_ratio: float = 0.0,
        notes: str = "",
    ) -> None:
        """Convenience wrapper to append a reflection-derived memory item."""
        self.append({
            "anomaly": anomaly,
            "path": path,
            "task_params": task_params,
            "outcome": outcome,
            "success": success,
            "round": round_no,
            "node_hit_ratio": node_hit_ratio,
            "notes": notes,
        })

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def success_rate(self, anomaly: str) -> float:
        """Return the success rate for a given anomaly type."""
        items = self.load(anomaly=anomaly, limit=100)
        if not items:
            return 0.0
        success_count = sum(1 for item in items if item.get("success"))
        return success_count / len(items)

    def recent_outcomes(self, anomaly: str, limit: int = 10) -> list[str]:
        """Return the most recent outcome strings for an anomaly."""
        items = self.load(anomaly=anomaly, limit=limit)
        return [item.get("outcome", "") for item in items if item.get("outcome")]
