"""Execution policy: which proposed steps are applied and which wait for review."""
from __future__ import annotations

from app.schemas import CleaningStep, PipelineOptions


def decide(step: CleaningStep, options: PipelineOptions) -> CleaningStep:
    """Low-risk steps are approved; anything else is skipped (unattended) or left proposed (interactive)."""
    if step.step_id in options.approved_step_ids:
        step.status, step.source = "approved", "user"
    elif step.risk == "low":
        step.status = "approved"
    elif options.policy == "unattended":
        step.status = "skipped_needs_review"
    else:
        step.status = "proposed"
    return step
