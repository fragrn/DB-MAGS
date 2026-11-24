"""Reflection strategy reserved for DBA-post reproduction."""

from __future__ import annotations

from ..models.reflection import ReflectionContext, ReflectionResult


class PostReflector:
    """Diagnose post/environment mismatches without chain-specific rules."""

    source = "post"

    def reflect(self, context: ReflectionContext, *, attempt: int = 1) -> ReflectionResult:
        if context.source != self.source:
            raise ValueError("PostReflector only accepts source='post'")

        missing = context.environment.get("missing_information", [])
        conflicts = context.environment.get("conflicts", [])
        if missing:
            diagnosis = "POST_CONTEXT_INCOMPLETE: required incident or environment information is missing"
            changes = {"request_missing_information": list(missing)}
        elif conflicts:
            diagnosis = "POST_CONTEXT_CONFLICT: post facts conflict with the current environment"
            changes = {"resolve_conflicts": list(conflicts)}
        else:
            diagnosis = "POST_REPRODUCTION_MISS: the current post-specific reproduction strategy did not match the observed result"
            changes = {"regenerate_strategy": True}

        return ReflectionResult(
            source="post",
            attempt=attempt,
            diagnosis=diagnosis,
            recommended_changes=changes,
            keep_successful_actions=[
                str(item["action_id"])
                for item in context.action_results
                if item.get("success") and item.get("action_id")
            ],
            retryable=True,
            failed_node=context.failed_node,
            failed_edge=context.failed_edge,
        )
