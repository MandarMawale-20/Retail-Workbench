"""Clean one dataset end to end: detect -> plan -> execute (shared by the pipeline and the idempotency check)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.cleaning.executor import execute_plan
from app.cleaning.plan_generator import build_plan
from app.llm.base import LLMProvider
from app.profiling.issue_detector import detect_issues
from app.profiling.profiler import profile_frame
from app.schemas import CleaningStep, Issue, PipelineOptions, RoleBinding


@dataclass
class CleanOutcome:
    frame: pd.DataFrame
    steps: list[CleaningStep]
    issues: list[Issue]
    failures: list[dict] = field(default_factory=list)
    exact_dups_removed: int = 0


def clean_dataset(dataset: str, version_id: str, frame: pd.DataFrame, bindings: list[RoleBinding],
                  options: PipelineOptions, *, reject_count: int = 0,
                  advisor_provider: LLMProvider | None = None,
                  provider: LLMProvider | None = None) -> CleanOutcome:
    profile = profile_frame(frame, dataset=dataset, version_id=version_id, stage="raw", reject_count=reject_count)
    detected = detect_issues(dataset, frame, profile, bindings)
    plan = build_plan(dataset, version_id, frame, profile, detected, bindings, options, advisor_provider,
                      provider=provider)
    result = execute_plan(frame, plan.steps)
    resolved = {s.issue_id for s in plan.steps if s.status == "applied"}
    for issue in plan.issues:
        if issue.issue_id in resolved:
            issue.status = "resolved"
    for issue in plan.issues:  # twin issues share the same applied step
        if issue.status == "planned":
            issue.status = "resolved"
    return CleanOutcome(result.frame, plan.steps, plan.issues, result.failures, result.exact_dups_removed)
