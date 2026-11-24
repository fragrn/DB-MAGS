"""M0 unit and acceptance tests.

These tests intentionally avoid LLMs, real experiments and external services.
They verify the foundation that later modules are required to use.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from db_repro_agent.config import load_config
from db_repro_agent.logging_config import configure_logging, log_context
from db_repro_agent.models.common import ExperimentId, ExperimentTimestamp, ResultStatus
from db_repro_agent.runtime.artifact_store import ArtifactStore


def test_config_loads_yaml_and_environment_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
database:
  host: db.example
  port: 3307
  database: incident_db
agent:
  max_attempts: 4
artifact_root: artifacts
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(
        config_file,
        environ={
            "DBMAGS_MYSQL_HOST": "127.0.0.1",
            "DBMAGS_MAX_HITL_ROUNDS": "5",
            "DBMAGS_ARTIFACT_ROOT": str(tmp_path / "env-artifacts"),
        },
    ).resolve_paths(tmp_path)

    assert config.database.host == "127.0.0.1"
    assert config.database.port == 3307
    assert config.database.database == "incident_db"
    assert config.agent.max_attempts == 4
    assert config.agent.max_hitl_rounds == 5
    assert config.artifact_root == (tmp_path / "env-artifacts").resolve()


def test_common_models_are_serializable() -> None:
    experiment_id = ExperimentId(value="post_004")
    timestamp = ExperimentTimestamp.now()

    assert str(experiment_id) == "post_004"
    assert timestamp.value.tzinfo is not None
    assert ResultStatus.EXPERIMENT_SUCCESS.value == "experiment_success"


def test_artifact_json_and_jsonl_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiment_runs")
    experiment_dir = store.create_experiment("post_004")
    relative_manifest = experiment_dir.relative_to(store.root) / "incident.json"
    incident = {"incident_id": "post_004", "symptom": "slow query", "attempt": 1}

    store.write_json(relative_manifest, incident)
    store.write_jsonl("post_004/events.jsonl", [{"event": "created"}, {"event": "checked"}])

    assert store.read_json(relative_manifest) == incident
    lines = (store.root / "post_004/events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"event": "created"}, {"event": "checked"}]


def test_attempt_directories_increment_per_experiment(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiment_runs")
    store.create_experiment("post_004")

    first = store.create_attempt_dir("post_004")
    second = store.create_attempt_dir("post_004")
    other = store.create_experiment("post_005")
    other_first = store.create_attempt_dir(other)

    assert first.name == "attempt_001"
    assert second.name == "attempt_002"
    assert other_first.name == "attempt_001"


def test_logging_contains_required_experiment_context(caplog) -> None:
    logger = configure_logging()
    logger.addHandler(caplog.handler)
    try:
        with log_context(
            experiment_id="post_004",
            phase="m0",
            attempt=2,
            module="artifact_store",
            action_id="write_json",
        ):
            logger.warning("artifact test")
    finally:
        logger.removeHandler(caplog.handler)

    record = next(record for record in caplog.records if record.message == "artifact test")
    assert record.experiment_id == "post_004"
    assert record.phase == "m0"
    assert record.attempt == "2"
    assert record.module == "artifact_store"
    assert record.action_id == "write_json"


def test_doctor_can_validate_configuration_without_database(tmp_path: Path) -> None:
    from db_repro_agent.cli import doctor

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"artifact_root": str(tmp_path / "runs")}), encoding="utf-8")
    assert doctor(config_file, skip_db=True) == 0
