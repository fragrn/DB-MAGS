"""Compatibility package for the renamed AnomalyGraphAgent code.

The codebase historically imported modules as ``agent.*``.  The source files
now live under ``AnomalyGraphAgent/``, so this package keeps old imports and
CLI commands working without duplicating modules.
"""

from pathlib import Path

_SOURCE_DIR = Path(__file__).resolve().parent.parent / "AnomalyGraphAgent"
__path__ = [str(_SOURCE_DIR)]

from agent.config import RuntimeConfig
from agent.runtime import DBMAGSRuntime
from agent.types import ExperimentRequest

__version__ = "1.0.0"

__all__ = [
    "RuntimeConfig",
    "DBMAGSRuntime",
    "ExperimentRequest",
    "__version__",
]
