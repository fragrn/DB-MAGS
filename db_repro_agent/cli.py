"""Command-line helpers for the M0 foundation."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from .config import load_config
from .logging_config import configure_logging, log_context
from .runtime.artifact_store import ArtifactStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="db-repro-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check configuration and database TCP reachability")
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument("--skip-db", action="store_true")
    return parser


def doctor(config_path: Path | None = None, *, skip_db: bool = False) -> int:
    logger = configure_logging()
    config = load_config(config_path).resolve_paths()
    store = ArtifactStore(config.artifact_root)
    with log_context(module="cli", phase="doctor"):
        logger.info("configuration loaded artifact_root=%s", store.root)
        if skip_db:
            logger.info("database connectivity check skipped")
            return 0
        try:
            with socket.create_connection(
                (config.database.host, config.database.port),
                timeout=config.database.connect_timeout_seconds,
            ):
                logger.info("database TCP connectivity is available")
            return 0
        except OSError as exc:
            logger.error("database TCP connectivity failed: %s", exc)
            return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args.config, skip_db=args.skip_db)
    raise AssertionError(f"unhandled command: {args.command}")
