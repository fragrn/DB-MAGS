"""Structured logging context shared by orchestration and runtime modules."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_context: ContextVar[dict[str, str]] = ContextVar("experiment_log_context", default={})


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        values = _context.get()
        for field in ("experiment_id", "phase", "attempt", "module", "action_id"):
            setattr(record, field, values.get(field, "-"))
        return True


@contextmanager
def log_context(**values: object) -> Iterator[None]:
    current = dict(_context.get())
    current.update({key: str(value) for key, value in values.items() if value is not None})
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("db_repro_agent")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(ContextFilter())
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s experiment=%(experiment_id)s "
                "phase=%(phase)s attempt=%(attempt)s module=%(module)s "
                "action=%(action_id)s %(message)s"
            )
        )
        logger.addHandler(handler)
    return logger
