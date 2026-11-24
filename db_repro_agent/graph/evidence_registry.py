"""Small deterministic registry of Evidence Rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..config import _parse_simple_yaml
from ..models.evidence import EvidenceRule


class EvidenceRuleRegistry:
    def __init__(self, rules: Mapping[str, EvidenceRule] | None = None, *, path: str | Path | None = None) -> None:
        if rules is not None and path is not None:
            raise ValueError("provide rules or path, not both")
        if rules is not None:
            self._rules = dict(rules)
        elif path is not None:
            self._rules = self._load(Path(path))
        else:
            self._rules = {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EvidenceRuleRegistry":
        return cls(path=path)

    def get(self, rule_id: str) -> EvidenceRule:
        try:
            return self._rules[rule_id]
        except KeyError as exc:
            raise KeyError(f"unknown evidence rule: {rule_id}") from exc

    def all(self) -> dict[str, EvidenceRule]:
        return dict(self._rules)

    @staticmethod
    def _load(path: Path) -> dict[str, EvidenceRule]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            raw = _parse_simple_yaml(text)
        if not isinstance(raw, dict):
            raise ValueError("evidence rule file must contain a mapping")
        return {
            rule_id: EvidenceRule.model_validate({"id": rule_id, **rule_data})
            for rule_id, rule_data in raw.items()
        }
