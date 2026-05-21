from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from agent_runtime.types import MemoryItem, ReflectionResult


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()
        items = []
        for line in lines[-limit:]:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    def append_item(self, item: MemoryItem | dict[str, Any]) -> None:
        payload = asdict(item) if is_dataclass(item) else item
        with self.path.open("a") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")

    def append_reflection(self, reflection: ReflectionResult, context: dict[str, Any]) -> None:
        for lesson in reflection.memory_update:
            self.append_item(
                MemoryItem(
                    dbms=str(context.get("dbms", "mysql")),
                    workload=str(context.get("workload", "tpcc")),
                    anomaly_type=str(context.get("anomaly_type", "combined")),
                    context=str(context.get("context", "")),
                    lesson=lesson,
                    evidence=context.get("evidence", {}),
                )
            )
