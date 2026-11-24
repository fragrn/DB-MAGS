"""Single owner of experiment and attempt artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from ..models.common import ErrorCode, ExperimentId, ReproductionError, utc_now


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_experiment(self, experiment_id: ExperimentId | str | None = None) -> Path:
        exp_id = _id_value(experiment_id or ExperimentId.new())
        path = self.root / exp_id
        path.mkdir(parents=True, exist_ok=False)
        self.write_json(
            path.relative_to(self.root) / "manifest.json",
            {"experiment_id": exp_id, "created_at": utc_now().isoformat(), "schema_version": 1},
        )
        return path

    def create_attempt_dir(self, experiment: str | Path, attempt: int | None = None) -> Path:
        experiment_dir = self._experiment_dir(experiment)
        experiment_dir.mkdir(parents=True, exist_ok=True)
        number = attempt if attempt is not None else self._next_attempt_number(experiment_dir)
        if number < 1:
            raise ReproductionError("attempt must be >= 1", code=ErrorCode.ARTIFACT)
        path = experiment_dir / f"attempt_{number:03d}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def write_json(self, relative_path: str | Path, value: Any) -> Path:
        path = self._safe_path(relative_path)
        payload = _to_jsonable(value)
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return path

    def write_jsonl(self, relative_path: str | Path, records: Iterable[Any]) -> Path:
        path = self._safe_path(relative_path)
        lines = [json.dumps(_to_jsonable(item), ensure_ascii=False, sort_keys=True) for item in records]
        self._atomic_write(path, ("\n".join(lines) + "\n") if lines else "")
        return path

    def read_json(self, relative_path: str | Path) -> Any:
        path = self._safe_path(relative_path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReproductionError(
                f"Unable to read JSON artifact: {path}",
                code=ErrorCode.ARTIFACT,
                details={"error": str(exc)},
            ) from exc

    def _experiment_dir(self, experiment: str | Path) -> Path:
        path = Path(experiment)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if path != self.root and self.root not in path.parents:
            raise ReproductionError("experiment path is outside artifact root", code=ErrorCode.ARTIFACT)
        return path

    def _safe_path(self, relative_path: str | Path) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            candidate = path.resolve()
        else:
            candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ReproductionError("artifact path is outside artifact root", code=ErrorCode.ARTIFACT)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            raise ReproductionError(
                f"Unable to write artifact: {path}", code=ErrorCode.ARTIFACT, details={"error": str(exc)}
            ) from exc

    @staticmethod
    def _next_attempt_number(experiment_dir: Path) -> int:
        numbers = []
        for child in experiment_dir.iterdir():
            if child.is_dir() and child.name.startswith("attempt_"):
                suffix = child.name.removeprefix("attempt_")
                if suffix.isdigit():
                    numbers.append(int(suffix))
        return max(numbers, default=0) + 1


def _id_value(value: ExperimentId | str) -> str:
    return value.value if isinstance(value, ExperimentId) else str(value)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
