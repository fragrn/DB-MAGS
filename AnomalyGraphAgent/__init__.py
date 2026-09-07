"""
DB-MAGS Single-Agent System.

A single global planner replaces the old GlobalPlanner + SpecialistAgent architecture.
Anomalies are constrained to a hardcoded propagation graph.
"""

from AnomalyGraphAgent.config import RuntimeConfig
from AnomalyGraphAgent.runtime import DBMAGSRuntime
from AnomalyGraphAgent.types import ExperimentRequest

__version__ = "1.0.0"

__all__ = [
    "RuntimeConfig",
    "DBMAGSRuntime",
    "ExperimentRequest",
    "__version__",
]
