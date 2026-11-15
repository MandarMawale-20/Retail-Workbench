"""Generate the cleaning plan (before any mutation) by dry-running approved steps on a copy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import pandas as pd

from app.cleaning.advisor import CleaningAdvisor
from app.cleaning.category import propose_mapping
from app.cleaning.executor import apply_step
from app.cleaning.policy import decide
from app.common import h
from app.llm.base import LLMProvider
from app.profiling.issue_detector import make_issue, role_map
from app.profiling.statistics import (
    analyze_dates,
    collapse_ws,
    non_null_values,
    null_tokens,
)
from app.roles.role_catalog import ID_ROLES
from app.schemas import CleaningStep, DatasetProfile, Issue, PipelineOptions

_REASONS = {
    "normalize_null_tokens": "Placeholder tokens such as 'N/A' or '-' represent missing values",
    "trim_whitespace": "Leading, trailing or repeated whitespace creates false variants",
    "normalize_case": "Values differ only by letter case",
    "format_sku": "Identifier spellings differ only by case, spaces or dashes",
    "parse_numeric": "Numbers carry currency symbols or thousands separators",
    "parse_date_iso": "Dates are not stored in ISO-8601 form",
    "dedup_exact_rows": "Rows are exact duplicates after normalisation",
}


@dataclass
class PlanResult:
    steps: list[CleaningStep]
    issues: list[Issue]


def _step(dataset: str, version_id: str, issue: Issue, op: str, columns: list[str], params: dict,
          risk: str, reason: str | None = None, confidence: float | None = None) -> CleaningStep:
    return CleaningStep(
        step_id="step_" + h(op, dataset, sorted(columns), params)[:12], issue_id=issue.issue_id,
        dataset_version_id=version_id, dataset=dataset, columns=columns, transformation=op,
        parameters=params,         reason=reason or _REASONS[op], risk=cast(Literal["low", "medium", "high"], risk),
        source="rule", confidence=confidence,
    )


def _set_status(issues: dict[str, Issue], step: CleaningStep) -> None:
    issue = issues[step.issue_id]
    issue.status = "planned" if step.status in ("approved", "proposed") else "needs_review"


def build_plan(dataset: str, version_id: str, frame: pd.DataFrame, profile: DatasetProfile,
               detected: list[Issue], bindings, options: PipelineOptions,
               advisor_provider: LLMProvider | None = None,
               *, provider: LLMProvider | None = None) -> PlanResult:
    """Plan steps per issue in fixed order; dependent decisions (aliases, duplicates) use a dry-run copy."""
    issues = {i.issue_id: i for i in detected}
    steps: list[CleaningStep] = []
    work = frame.copy()
    roles = role_map(dataset, bindings)
    types = {c.name: c.inferred_type for c in profile.columns}
    tokens = sorted(null_tokens())

    def add(step: CleaningStep) -> None:
        nonlocal work
        decide(step, options)
        steps.append(step)
        _set_status(issues, step)
        if step.status == "approved":
            work, _, _ = apply_step(work, step)

    by_type: dict[str, list[Issue]] = {}
    for issue in detected:
        by_type.setdefault(issue.issue_type, []).append(issue)

    for issue in by_type.get("null_tokens", []):
        if issue.column is not None:
            add(_step(dataset, version_id, issue, "normalize_null_tokens", [issue.column], {"tokens": tokens}, "low"))
    for issue in by_type.get("whitespace", []):
        if issue.column is not None:
            add(_step(dataset, version_id, issue, "trim_whitespace", [issue.column], {}, "low"))
    for issue in by_type.get("case_variants", []):
        if issue.column is not None:
            op = "format_sku" if roles.get(issue.column) in ID_ROLES else "normalize_case"
            add(_step(dataset, version_id, issue, op, [issue.column], {}, "low"))

    for column, role in sorted(roles.items()):
        if role != "category" or types.get(column) != "string":
            continue
        counts = _value_counts(work[column])
        proposal = propose_mapping(counts)
        active_provider = provider or advisor_provider
        if active_provider is not None and options.use_advisor:
            advisor = CleaningAdvisor(active_provider).suggest(counts)
            proposal.applied.update({k: v for k, v in advisor.applied.items() if k not in proposal.applied})
            proposal.review.extend(advisor.review)
        if not proposal.affected_values():
            continue
        affected = sum(counts[v] for v in proposal.affected_values() if v in counts)
        examples = [f"{a} -> {m['to']}" for a, m in proposal.applied.items()] + \
                   [" ~ ".join(r["values"]) for r in proposal.review]
        issue = make_issue(dataset, "category_variants", column, "medium", affected, examples)
        issues[issue.issue_id] = issue
        if proposal.applied:
            mapping = {k: v["to"] for k, v in sorted(proposal.applied.items())}
            conf = min(v["confidence"] for v in proposal.applied.values())
            add(_step(dataset, version_id, issue, "normalize_category_alias", [column], {"mapping": mapping},
                      "low", f"Category variants map to canonical labels (alias table / fuzzy >= 0.92): {len(mapping)} values",
                      conf))
        if proposal.review:
            mapping = {r["values"][1]: r["target"] for r in proposal.review}
            conf = min(r["confidence"] for r in proposal.review)
            add(_step(dataset, version_id, issue, "normalize_category_alias", [column], {"mapping": mapping},
                      "medium", "Plausible but uncertain category merges need human confirmation", conf))

    numeric_columns: set[str] = set()
    for issue in by_type.get("mixed_numeric", []) + by_type.get("numeric_parse_failures", []):
        if issue.column is None:
            continue
        if issue.column in numeric_columns:
            _set_status(issues, next(s for s in steps if issue.column in s.columns and s.transformation == "parse_numeric"))
            continue
        numeric_columns.add(issue.column)
        risk = "medium" if types.get(issue.column) == "mixed" else "low"
        add(_step(dataset, version_id, issue, "parse_numeric", [issue.column], {}, risk))
        for twin in by_type.get("mixed_numeric", []) + by_type.get("numeric_parse_failures", []):
            if twin.column == issue.column and twin is not issue:
                twin.status = issues[issue.issue_id].status

    date_columns: set[str] = set()
    for issue in by_type.get("date_format_ambiguous", []):
        if issue.column is not None:
            add(_step(dataset, version_id, issue, "parse_date_iso", [issue.column], {"convention": "ambiguous"}, "high",
                      "DD/MM vs MM/DD cannot be determined from the data; interpretation is never guessed"))
            date_columns.add(issue.column)
    for issue in by_type.get("non_iso_dates", []) + by_type.get("date_parse_failures", []):
        if issue.column is None:
            continue
        if issue.column in date_columns:
            continue
        date_columns.add(issue.column)
        analysis = analyze_dates([collapse_ws(v) for v in non_null_values(work[issue.column])])
        add(_step(dataset, version_id, issue, "parse_date_iso", [issue.column],
                  {"convention": analysis.kind, "has_time": analysis.has_time}, "low"))
        for twin in by_type.get("non_iso_dates", []) + by_type.get("date_parse_failures", []):
            if twin.column == issue.column and twin is not issue:
                twin.status = issues[issue.issue_id].status

    if len(work) and int(work.duplicated().sum()):
        removed = int(work.duplicated().sum())
        issue = make_issue(dataset, "exact_duplicate_rows", None, "medium", removed)
        issues[issue.issue_id] = issue
        add(_step(dataset, version_id, issue, "dedup_exact_rows", list(frame.columns), {}, "low"))

    return PlanResult(steps=steps, issues=sorted(issues.values(), key=lambda i: i.issue_id))


def _value_counts(series: pd.Series):
    from collections import Counter
    return Counter(v for v in series.tolist() if v is not None)
