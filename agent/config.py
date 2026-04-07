from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_ENV_FILES = (".env", ".env.local")


def load_dotenv_files(base_dir: str | os.PathLike[str] | None = None, filenames: Iterable[str] = DEFAULT_ENV_FILES) -> None:
    roots = []
    if base_dir:
        roots.append(Path(base_dir))
    roots.extend([Path.cwd(), Path(__file__).resolve().parents[1]])
    seen = set()
    for root in roots:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        for filename in filenames:
            path = root / filename
            if not path.exists() or not path.is_file():
                continue
            _load_file(path)


def _load_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class RuntimeConfig:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.vectorengine.ai"
    openai_model: str = "gpt-5.4-nano-2026-03-17"
    planner_enabled: bool = True

    @classmethod
    def from_env(cls, base_dir: str | os.PathLike[str] | None = None) -> "RuntimeConfig":
        load_dotenv_files(base_dir=base_dir)
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.vectorengine.ai"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-nano-2026-03-17"),
            planner_enabled=os.getenv("ENABLE_OPENAI_PLANNER", "1") != "0",
        )
