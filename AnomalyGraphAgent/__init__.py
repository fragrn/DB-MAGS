"""
DB-MAGS Single-Agent System.

A single global planner replaces the old GlobalPlanner + SpecialistAgent architecture.
Anomalies are constrained to a hardcoded propagation graph.
"""

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
