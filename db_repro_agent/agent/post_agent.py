"""M10 Post Agent interface; incident analysis begins in M13."""

from __future__ import annotations

from ..models.reflection import ReflectionContext, ReflectionResult
from .contracts import ExperimentResult, PostReproductionRequest
from .post_reflector import PostReflector


class PostAgent:
    """Stable placeholder for the future DBA Post reproduction pipeline."""

    def __init__(self, *, reflector: PostReflector | None = None) -> None:
        self.reflector = reflector or PostReflector()

    def reflect(self, context: ReflectionContext, *, attempt: int = 1) -> ReflectionResult:
        """Expose the Post-only Reflection boundary for later M13+ integration."""
        return self.reflector.reflect(context, attempt=attempt)

    def reproduce_post(self, request: PostReproductionRequest) -> ExperimentResult:
        return ExperimentResult(
            incident_id=request.incident_id,
            agent="post",
            status="not_implemented",
            message="Post analysis and Direct/Propagation selection start in M13",
        )
