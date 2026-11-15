"""Deterministic plan validator (the real defence): scope, columns, types, values, metrics, joins, limits, tool evidence."""
from __future__ import annotations

from collections.abc import Callable

from rapidfuzz import process

from app.agents.planner_tools import is_pii_column
from app.cleaning.category import alias_index, match_key
from app.common import looks_like_injection, normalize_value
from app.config import thresholds
from app.errors import AppError
from app.metrics.binder import try_bind
from app.query.analysis import (
    build_measures,
    column_ids_in_plan,
    plan_fact_joins,
    plan_list_joins,
)
from app.query.catalog import Catalog
from app.query.schemas import (
    ExecutionResult,
    QueryPlan,
    RequiredEvidence,
    ToolLog,
    ValidationIssue,
    ValidationResult,
)
from app.schemas import ExecutionContext

ValuesProvider = Callable[[str, str], list[tuple[str, int]]]
_ORDERING_OPS = {"gt", "gte", "lt", "lte", "between"}
_ = ExecutionResult  # re-exported for type hints elsewhere


def required_evidence(plan: QueryPlan, catalog: Catalog) -> RequiredEvidence:
    """Tool evidence the plan itself implies (see AGENTS.md A4)."""
    distinct: list[str] = []
    for f in plan.filters:
        info = catalog.column(f.column_id)
        if f.op in ("eq", "in", "contains", "neq") and info is not None and info.is_text and f.column_id not in distinct:
            distinct.append(f.column_id)
    uncertain = []
    for ref in plan.metrics:
        binding = try_bind(ref.metric_id, catalog.role_index)
        if binding and binding.binding_uncertain and ref.metric_id not in uncertain:
            uncertain.append(ref.metric_id)
    return RequiredEvidence(list_distinct=distinct, preview_join=sorted(set(plan.relationship_ids)),
                            get_metric_definition=uncertain)


def missing_evidence(required: RequiredEvidence, tool_log: ToolLog) -> list[ValidationIssue]:
    issues = [ValidationIssue(code="TOOL_EVIDENCE_MISSING", path="filters", message=f"call list_distinct for {c} before filtering on it",
                              details={"tool": "list_distinct", "args": {"column_id": c}}) for c in required.list_distinct
              if not tool_log.has("list_distinct", column_id=c)]
    issues += [ValidationIssue(code="TOOL_EVIDENCE_MISSING", path="relationship_ids",
                               message=f"call preview_join for {r} before joining", details={"tool": "preview_join", "args": {"relationship_id": r}})
               for r in required.preview_join if not tool_log.has("preview_join", relationship_id=r)]
    issues += [ValidationIssue(code="TOOL_EVIDENCE_MISSING", path="metrics",
                               message=f"call get_metric_definition for {m} (binding is uncertain)",
                               details={"tool": "get_metric_definition", "args": {"metric_id": m}})
               for m in required.get_metric_definition if not tool_log.has("get_metric_definition", metric_id=m)]
    return issues


def _suggest(value: str, candidates: list[str]) -> list[str]:
    key = match_key(value)
    out = []
    canonical = alias_index().get(key)
    if canonical and canonical in candidates:
        out.append(canonical)
    out += [c for c, score, _ in process.extract(value, candidates, limit=3, score_cutoff=60) if c not in out]
    return out[:3]


def validate_plan(plan: QueryPlan, ctx: ExecutionContext, catalog: Catalog, tool_log: ToolLog,
                  values: ValuesProvider, *, enforce_tools: bool = True) -> ValidationResult:
    """Run checks in documented order; collect all errors from the first failing group."""
    if catalog.doc.owner_id != ctx.owner_id:
        raise AppError("ISOLATION_VIOLATION", "run belongs to a different owner", stage="auth")
    errors: list[ValidationIssue] = []
    limits = thresholds()["limits"]

    def err(code: str, path: str, message: str, suggestion: str | None = None, **details) -> None:
        errors.append(ValidationIssue(code=code, path=path, message=message, suggestion=suggestion, details=details))

    # 1-2 scope
    if set(ctx.active_dataset_version_ids) != set(catalog.version_ids().values()):
        err("SCOPE_VIOLATION", "datasets", "session dataset versions do not match the pinned run")
    for ds in plan.datasets:
        if ds not in catalog.datasets:
            err("SCOPE_VIOLATION", "datasets", f"dataset '{ds}' is not part of the active run")
    if len(set(plan.datasets) | {c.split('.')[0] for c in column_ids_in_plan(plan)}) > limits["max_datasets"]:
        err("LIMIT_EXCEEDED", "datasets", f"at most {limits['max_datasets']} datasets per query")
    if errors:
        return _result(errors, plan, catalog)

    # 3 columns
    all_columns = [c.column_id for c in catalog.all_columns()]
    for path, col in [(f"filters[{i}]", f.column_id) for i, f in enumerate(plan.filters)] + \
                     [(f"date_ranges[{i}]", r.column_id) for i, r in enumerate(plan.date_ranges)] + \
                     [(f"date_groupings[{i}]", g.column_id) for i, g in enumerate(plan.date_groupings)] + \
                     [("group_by", c) for c in plan.group_by] + [("select_columns", c) for c in plan.select_columns] + \
                     [(f"aggregations[{i}]", a.column_id) for i, a in enumerate(plan.aggregations) if a.column_id]:
        if catalog.column(col) is None:
            near = process.extractOne(col, all_columns, score_cutoff=60)
            err("UNKNOWN_COLUMN", path, f"column '{col}' does not exist", near[0] if near else None)
    if errors:
        return _result(errors, plan, catalog)

    for path, col in [("group_by", c) for c in plan.group_by] + [("select_columns", c) for c in plan.select_columns]:
        if is_pii_column(col.split(".", 1)[1]):
            err("PII_COLUMN", path, f"'{col}' holds personal data and cannot be listed or grouped")
    raw = plan.dataset_stage == "raw"
    if raw:
        if plan.metrics or plan.relationship_ids:
            err("STAGE_RULE", "dataset_stage", "raw stage allows single-table counts, distincts, filters and grouping only")
        if len({c.split('.')[0] for c in column_ids_in_plan(plan)}) > 1:
            err("STAGE_RULE", "datasets", "raw stage cannot join datasets")
        for i, a in enumerate(plan.aggregations):
            if a.function in ("sum", "avg"):
                err("STAGE_RULE", f"aggregations[{i}]", "numeric aggregation is not allowed on raw (all-text) data")
        for i, f in enumerate(plan.filters):
            if f.op in _ORDERING_OPS:
                err("STAGE_RULE", f"filters[{i}]", "ordering comparisons are not allowed on raw (all-text) data")
        if plan.date_groupings or plan.date_ranges:
            err("STAGE_RULE", "dataset_stage", "date grouping and date ranges require resolved clean-stage dates")

    # 4 types and ambiguous dates
    for i, f in enumerate(plan.filters):
        info = catalog.column(f.column_id)
        if info is None:
            continue
        if not raw and info.date_ambiguous:
            err("AMBIGUOUS_DATE", f"filters[{i}]", f"'{f.column_id}' has an ambiguous date format (DD/MM vs MM/DD); it was not interpreted")
        elif not raw and f.op in _ORDERING_OPS and not (info.is_numeric or info.is_date):
            err("TYPE_MISMATCH", f"filters[{i}]", f"operator '{f.op}' needs a numeric or date column")
        elif f.op == "contains" and not info.is_text:
            err("TYPE_MISMATCH", f"filters[{i}]", "operator 'contains' needs a text column")
        if f.op in ("in", "between") and not isinstance(f.value, list):
            err("INVALID_PLAN", f"filters[{i}]", f"operator '{f.op}' needs a list value")
        if f.op == "between" and isinstance(f.value, list) and len(f.value) != 2:
            err("INVALID_PLAN", f"filters[{i}]", "between needs exactly two values")
        if isinstance(f.value, list) and len(f.value) > limits["max_filter_values"]:
            err("LIMIT_EXCEEDED", f"filters[{i}]", "too many filter values")
    for i, r in enumerate(plan.date_ranges):
        info = catalog.column(r.column_id)
        if info and not info.is_date:
            err("TYPE_MISMATCH", f"date_ranges[{i}]", f"'{r.column_id}' is not a resolved date column")
        if r.start > r.end:
            err("INVALID_PLAN", f"date_ranges[{i}]", "start must not be after end")
    for i, a in enumerate(plan.aggregations):
        info = catalog.column(a.column_id) if a.column_id else None
        if a.function in ("sum", "avg") and not raw and (info is None or not info.is_numeric):
            err("TYPE_MISMATCH", f"aggregations[{i}]", f"'{a.function}' needs a numeric column")
        if a.function != "count" and a.column_id is None:
            err("INVALID_PLAN", f"aggregations[{i}]", f"'{a.function}' needs a column_id")
    if not raw:
        for path, col in [("group_by", c) for c in plan.group_by] + [("select_columns", c) for c in plan.select_columns]:
            info = catalog.column(col)
            if info and info.date_ambiguous:
                err("AMBIGUOUS_DATE", path, f"'{col}' has an ambiguous date format; it was not interpreted")
        for i, g in enumerate(plan.date_groupings):
            info = catalog.column(g.column_id)
            if info and (not info.is_date or info.date_ambiguous):
                err("AMBIGUOUS_DATE" if info.date_ambiguous else "TYPE_MISMATCH",
                    f"date_groupings[{i}]", f"'{g.column_id}' is not a resolved date column")
        for i, r in enumerate(plan.date_ranges):
            info = catalog.column(r.column_id)
            if info and info.date_ambiguous:
                err("AMBIGUOUS_DATE", f"date_ranges[{i}]", f"'{r.column_id}' has an ambiguous date format; it was not interpreted")

    # 5 filter values must exist
    for i, f in enumerate(plan.filters):
        info = catalog.column(f.column_id)
        if info is None or not info.is_text or f.op not in ("eq", "neq", "in") or f.value is None:
            continue
        existing = values(f.column_id, plan.dataset_stage)
        known = {normalize_value(v): v for v, _ in existing}
        wanted = f.value if isinstance(f.value, list) else [f.value]
        for w in wanted:
            if normalize_value(w) not in known:
                suggestions = _suggest(str(w), [v for v, _ in existing])
                shown = str(w) if len(str(w)) <= 40 and not looks_like_injection(str(w)) else "the requested value"
                err("VALUE_NOT_FOUND", f"filters[{i}]", f"'{shown}' is not a value of {f.column_id}",
                    f"did you mean: {', '.join(suggestions)}" if suggestions else None,
                    column_id=f.column_id, near=suggestions)

    # 6 metrics + intent shape
    measures = []
    try:
        measures = build_measures(plan, catalog)
    except AppError as exc:
        err("UNSUPPORTED_METRIC", "metrics", exc.message, **(exc.details or {}))
    aliases = [m.alias for m in measures]
    if len(set(aliases)) != len(aliases):
        err("INVALID_PLAN", "metrics", "duplicate output aliases")
    if plan.intent in ("aggregate", "top_n", "compare") and not measures:
        err("INVALID_PLAN", "intent", f"intent '{plan.intent}' needs a metric or aggregation")
    if plan.intent in ("list", "distinct") and not plan.select_columns:
        err("INVALID_PLAN", "select_columns", f"intent '{plan.intent}' needs select_columns")
    outputs = set(aliases) | set(plan.group_by) | set(plan.select_columns) | {m.metric_id for m in plan.metrics}
    for i, s in enumerate(plan.sort):
        if s.field not in outputs:
            err("INVALID_PLAN", f"sort[{i}]", f"sort field '{s.field}' is not an output column or alias")

    # 7 relationships
    for rid in plan.relationship_ids:
        rel = catalog.graph.get(rid)
        if rel is None:
            err("UNKNOWN_RELATIONSHIP", "relationship_ids", f"relationship '{rid}' does not exist in the pinned graph")
        elif rel.status != "verified":
            err("UNSAFE_JOIN", "relationship_ids", f"relationship {rel.label()} is {rel.status}: {'; '.join(rel.reasons)}",
                relationship_id=rid, reasons=rel.reasons)
    if not errors and not raw:
        try:
            if measures:
                plan_fact_joins(plan, catalog, measures)
            elif len({c.split('.')[0] for c in column_ids_in_plan(plan)} | set(plan.datasets)) > 1:
                plan_list_joins(plan, catalog)
        except AppError as exc:
            suggested = (exc.details or {}).get("suggested_relationship_ids")
            err(exc.code, "relationship_ids", exc.message,
                f"add relationship_ids: {', '.join(suggested)}" if suggested else None, **(exc.details or {}))
    if errors:
        return _result(errors, plan, catalog, measures)

    # 8 tool evidence
    if enforce_tools:
        errors.extend(missing_evidence(required_evidence(plan, catalog), tool_log))
    return _result(errors, plan, catalog, measures)


def _result(errors: list[ValidationIssue], plan: QueryPlan, catalog: Catalog, measures=None) -> ValidationResult:
    used = {c.split(".")[0] for c in column_ids_in_plan(plan)} | set(plan.datasets)
    for m in measures or []:
        used |= {t.dataset for t in m.terms.values()}
    versions = {d: v for d, v in catalog.version_ids().items() if d in used}
    return ValidationResult(valid=not errors, errors=errors, bound_dataset_versions=versions,
                            bound_relationships=sorted(plan.relationship_ids),
                            bound_metrics=[m.binding.model_dump() for m in measures or [] if m.binding])
