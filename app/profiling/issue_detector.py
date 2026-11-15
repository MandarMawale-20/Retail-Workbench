"""Detect data-quality issues on a raw all-string frame. Detection never mutates data."""
from __future__ import annotations

import re
from collections import Counter
from typing import Literal

import pandas as pd

from app.common import h, sanitize_for_prompt
from app.config import thresholds
from app.profiling.statistics import (
    analyze_dates,
    collapse_ws,
    infer_type,
    is_canonical_number,
    non_null_values,
    null_tokens,
    parse_number,
    render_date,
)
from app.roles.role_catalog import CATEGORICAL_ROLES, ID_ROLES, NUMERIC_ROLES
from app.roles.role_inference import is_id_like
from app.schemas import ColumnProfile, DatasetProfile, Issue, IssueType, RoleBinding

_FORMATTED_NUMBER = re.compile(r"[₹$€£¥(),\s]|rs\.?|inr|usd|eur", re.IGNORECASE)


def make_issue(dataset: str, issue_type: IssueType, column: str | None,
               severity: Literal["low", "medium", "high"], count: int,
               examples: list[str] | None = None,
               status: Literal["detected", "planned", "resolved", "needs_review"] = "detected") -> Issue:
    return Issue(
        issue_id="iss_" + h(dataset, issue_type, sorted([column] if column else []))[:12],
        dataset=dataset, column=column, issue_type=issue_type, severity=severity, count=count,
        examples=[sanitize_for_prompt(e) for e in (examples or [])[:5]], status=status,
    )


def role_map(dataset: str, bindings: list[RoleBinding]) -> dict[str, str]:
    """column name -> role for usable (verified/candidate) bindings of one dataset."""
    prefix = dataset + "."
    return {b.column_id[len(prefix):]: b.role for b in bindings
            if b.column_id.startswith(prefix) and b.status in ("verified", "candidate")}


def is_categorical(col: ColumnProfile, role: str | None, rows: int) -> bool:
    if role in CATEGORICAL_ROLES or role in ID_ROLES:
        return True
    return col.inferred_type == "string" and 0 < col.distinct_count <= 50 and col.distinct_count <= 0.2 * max(rows, 1)


def _variant_groups(values: list[str], key) -> dict[str, Counter[str]]:
    groups: dict[str, Counter[str]] = {}
    for v in values:
        groups.setdefault(key(v), Counter())[v] += 1
    return {k: c for k, c in groups.items() if len(c) > 1}


def detect_issues(dataset: str, frame: pd.DataFrame, profile: DatasetProfile,
                  bindings: list[RoleBinding]) -> list[Issue]:
    """Column- and dataset-level issues on the raw frame (alias and duplicate issues come from planning)."""
    issues: list[Issue] = []
    if profile.structural_reject_count:
        issues.append(make_issue(dataset, "structural_rejects", None, "medium", profile.structural_reject_count))
    if profile.row_count == 0:
        issues.append(make_issue(dataset, "header_only", None, "medium", 0))
        return issues
    tokens = null_tokens()
    roles = role_map(dataset, bindings)
    th = thresholds()
    for col in profile.columns:
        series, role = frame[col.name], roles.get(col.name)
        raw_values = [v for v in series.tolist() if v is not None]
        token_cells = [v for v in raw_values if v.strip().casefold() in tokens]
        values = non_null_values(series, tokens)
        if not values:
            issues.append(make_issue(dataset, "all_null_column", col.name, "medium", len(series)))
            continue
        if token_cells:
            issues.append(make_issue(dataset, "null_tokens", col.name, "low", len(token_cells), token_cells))
        padded = [v for v in values if v != collapse_ws(v)]
        if padded:
            issues.append(make_issue(dataset, "whitespace", col.name, "low", len(padded), [repr(p) for p in padded]))
        trimmed = [collapse_ws(v) for v in values]
        id_like = is_id_like(col.name, role)
        if is_categorical(col, role, profile.row_count):
            key = (lambda v: re.sub(r"[^A-Z0-9]", "", v.upper())) if role in ID_ROLES else str.casefold
            groups = _variant_groups(trimmed, key)
            if groups:
                changed = sum(sum(c.values()) - max(c.values()) for c in groups.values())
                examples = [" / ".join(sorted(c)) for c in groups.values()]
                issues.append(make_issue(dataset, "case_variants", col.name, "low", changed, examples))
        if col.inferred_type in ("integer", "decimal", "mixed") and not id_like:
            issues.extend(_numeric_issues(dataset, col, role, values))
        if col.inferred_type == "date" or (col.inferred_type == "mixed" and analyze_dates(values).date_like_ratio > th["mixed_type_ratio"]):
            issues.extend(_date_issues(dataset, col.name, values))
        if col.inferred_type == "mixed" and not issues_for(issues, col.name):
            issues.append(make_issue(dataset, "mixed_numeric", col.name, "medium", len(values)))
    return issues


def issues_for(issues: list[Issue], column: str) -> list[Issue]:
    return [i for i in issues if i.column == column]


def _numeric_issues(dataset: str, col: ColumnProfile, role: str | None, values: list[str]) -> list[Issue]:
    out: list[Issue] = []
    parsed = [(v, parse_number(v)) for v in values]
    formatted = [v for v, p in parsed if p is not None and (_FORMATTED_NUMBER.search(v.strip()) or not is_canonical_number(v.strip()) and "," in v)]
    failures = [v for v, p in parsed if p is None]
    if formatted:
        out.append(make_issue(dataset, "mixed_numeric", col.name, "low", len(formatted), formatted))
    if failures:
        severity: Literal["medium", "high"] = "medium" if col.inferred_type != "mixed" else "high"
        out.append(make_issue(dataset, "numeric_parse_failures", col.name, severity, len(failures), failures))
    if role in NUMERIC_ROLES and any(p is not None and p < 0 for _, p in parsed):
        negatives = [v for v, p in parsed if p is not None and p < 0]
        out.append(make_issue(dataset, "negative_values", col.name, "medium", len(negatives), negatives))
    return out


def _date_issues(dataset: str, column: str, values: list[str]) -> list[Issue]:
    analysis = analyze_dates(values)
    stripped = [collapse_ws(v) for v in values]
    if analysis.kind in ("ambiguous", "conflicting"):
        ambiguous = [v for v in stripped if re.match(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}$", v)]
        return [make_issue(dataset, "date_format_ambiguous", column, "high", len(ambiguous), ambiguous)]
    out: list[Issue] = []
    if analysis.kind in ("iso", "dayfirst", "monthfirst"):
        non_iso = [v for v in stripped if render_date(v, analysis) not in (None, v)]
        if non_iso:
            out.append(make_issue(dataset, "non_iso_dates", column, "low", len(non_iso), non_iso))
        bad = [v for v in stripped if render_date(v, analysis) is None]
        if bad:
            out.append(make_issue(dataset, "date_parse_failures", column, "medium", len(bad), bad))
    elif analysis.kind == "none":
        out.append(make_issue(dataset, "date_parse_failures", column, "high", analysis.failures))
    return out


def detect_duplicate_ids(dataset: str, frame: pd.DataFrame, bindings: list[RoleBinding]) -> list[Issue]:
    """Flag (never delete) duplicated key values in id-role columns that otherwise look like keys."""
    out: list[Issue] = []
    key_ratio = thresholds()["key_candidate_ratio"]
    for column, role in sorted(role_map(dataset, bindings).items()):
        if role not in ID_ROLES:
            continue
        values = [v for v in frame[column].tolist() if v is not None]
        if not values:
            continue
        counts = Counter(values)
        dupes = {k: n for k, n in counts.items() if n > 1}
        if dupes and len(counts) / len(values) >= key_ratio:
            out.append(make_issue(dataset, "duplicate_ids", column, "high", sum(dupes.values()) - len(dupes),
                                  sorted(dupes), status="needs_review"))
    return out


def unresolved_role_issues(dataset: str, bindings: list[RoleBinding]) -> list[Issue]:
    prefix = dataset + "."
    return [make_issue(dataset, "unresolved_role", b.column_id[len(prefix):], "low", 1, [b.role], status="needs_review")
            for b in bindings if b.status == "ambiguous" and b.column_id.startswith(prefix)]


def inferred_types(frame: pd.DataFrame) -> dict[str, str]:
    return {c: infer_type(non_null_values(frame[c])) for c in frame.columns}
