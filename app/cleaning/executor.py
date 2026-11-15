"""Apply approved cleaning steps in the fixed order, recording rows affected and parse failures."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.cleaning.transformations import EXECUTION_ORDER, REGISTRY
from app.errors import AppError
from app.schemas import CleaningStep


@dataclass
class ExecutionOutput:
    frame: pd.DataFrame
    failures: list[dict] = field(default_factory=list)
    exact_dups_removed: int = 0


def apply_step(frame: pd.DataFrame, step: CleaningStep) -> tuple[pd.DataFrame, int, list[dict]]:
    fn = REGISTRY.get(step.transformation)
    if fn is None:
        raise AppError("CLEANING_VALIDATION_FAILED", f"unregistered transformation '{step.transformation}'", stage="clean")
    result = fn(frame, step.columns, step.parameters)
    return result.frame, result.rows_affected, [{**f, "dataset": step.dataset, "step_id": step.step_id} for f in result.failures]


def execute_plan(frame: pd.DataFrame, steps: list[CleaningStep]) -> ExecutionOutput:
    """Run every `approved` step in `EXECUTION_ORDER`; mark each `applied` (or `failed` and raise)."""
    out = ExecutionOutput(frame=frame.copy())
    rank = {name: i for i, name in enumerate(EXECUTION_ORDER)}
    for step in sorted((s for s in steps if s.status == "approved"), key=lambda s: (rank[s.transformation], s.step_id)):
        try:
            out.frame, step.rows_affected, failures = apply_step(out.frame, step)
        except Exception as exc:
            step.status = "failed"
            raise AppError("CLEANING_VALIDATION_FAILED", f"step {step.step_id} failed: {exc}", stage="clean") from exc
        step.status = "applied"
        out.failures.extend(failures)
        if step.transformation == "dedup_exact_rows":
            out.exact_dups_removed += step.rows_affected or 0
    return out
