from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any


SQL_DANGEROUS_KEYWORDS = {"drop", "truncate", "delete", "grant", "revoke"}


def to_pretty_json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, ensure_ascii=True, indent=2)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "task"


def contains_dangerous_sql(sql: str) -> bool:
    lowered = sql.lower()
    return any(keyword in lowered for keyword in SQL_DANGEROUS_KEYWORDS)
