from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


DEFAULT_ENV_FILES = (".env", ".env.local")


def load_dotenv_files(base_dir: str | os.PathLike[str] | None = None, filenames: Iterable[str] = DEFAULT_ENV_FILES) -> None:
    root = Path(base_dir or Path.cwd())
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
            line = line[len("export "):].strip()
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
