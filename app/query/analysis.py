"""Shared plan analysis: measures, fact groups and safe join planning (used by validator and compiler)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import load_yaml
from app.errors import AppError
from app.metrics.binder import MetricBinding, bind_metric
from app.metrics.definitions import MetricVariant, get_metric
from app.query.catalog import Catalog
from app.query.schemas import Aggregation, QueryPlan
from app.relationships.join_policy import JoinEdge, plan_joins


@dataclass
class TermUse:
    measure_index: int
    term: str
    dataset: str
    agg: str                       # sum | count_distinct | count_rows | count_status | count | avg | min | max
    columns: list[str]             # column ids
    status_values: list[str] = field(default_factory=list)


@dataclass
class Measure:
    alias: str
    unit: str
    kind: str                      # metric | aggregation
    expression: tuple
    terms: dict[str, TermUse]
    metric_id: str | None = None
    binding: MetricBinding | None = None


def column_ids_in_plan(plan: QueryPlan) -> list[str]:
    ids = ([f.column_id for f in plan.filters] + [r.column_id for r in plan.date_ranges]
           + [g.column_id for g in plan.date_groupings] + list(plan.group_by) + list(plan.select_columns))
    ids += [a.column_id for a in plan.aggregations if a.column_id]
    return ids


def _variant(binding: MetricBinding) -> MetricVariant:
    metric = get_metric(binding.metric_id)
    assert metric is not None
    return next(v for v in metric.variants if v.variant_id == binding.variant_id)


def build_measures(plan: QueryPlan, catalog: Catalog) -> list[Measure]:
    """Measures for the plan's metrics and aggregations; raises UNSUPPORTED_QUERY on missing roles."""
    measures: list[Measure] = []
    status_values = [str(v).casefold() for v in load_yaml("metrics.yaml")["return_status_values"]]
    for ref in plan.metrics:
        binding = bind_metric(ref.metric_id, catalog.role_index)
        variant, metric = _variant(binding), get_metric(ref.metric_id)
        assert metric is not None
        idx = len(measures)
        terms = {name: TermUse(idx, name, binding.term_datasets[name], spec.agg, binding.term_columns.get(name, []),
                               status_values if spec.agg == "count_status" else [])
                 for name, spec in variant.terms.items()}
        measures.append(Measure(ref.alias or ref.metric_id, metric.unit, "metric", variant.expression, terms,
                                ref.metric_id, binding))
    for agg in plan.aggregations:
        idx = len(measures)
        dataset = agg.column_id.split(".", 1)[0] if agg.column_id else (plan.datasets[0] if plan.datasets else "")
        func = {"count": "count_rows" if agg.column_id is None else "count", "distinct_count": "count_distinct"}.get(agg.function, agg.function)
        term = TermUse(idx, "v", dataset, func, [agg.column_id] if agg.column_id else [])
        unit = "count" if agg.function in ("count", "distinct_count") else "ratio"
        measures.append(Measure(agg.alias, unit, "aggregation", ("ref", "v"), {"v": term}))
    return measures


def fact_datasets(measures: list[Measure]) -> list[str]:
    seen: list[str] = []
    for m in measures:
        for t in m.terms.values():
            if t.dataset not in seen:
                seen.append(t.dataset)
    return seen


def dimension_datasets(plan: QueryPlan) -> set[str]:
    cols = (list(plan.group_by) + [g.column_id for g in plan.date_groupings]
            + [f.column_id for f in plan.filters] + [r.column_id for r in plan.date_ranges])
    return {c.split(".", 1)[0] for c in cols}


def plan_fact_joins(plan: QueryPlan, catalog: Catalog, measures: list[Measure]) -> dict[str, list[JoinEdge]]:
    """Join edges per fact dataset, restricted to the relationship ids the plan declares."""
    allowed = set(plan.relationship_ids)
    targets = dimension_datasets(plan)
    return {fact: plan_joins(catalog.graph, fact, targets, allowed) for fact in fact_datasets(measures)}


def root_dataset(plan: QueryPlan, catalog: Catalog) -> str:
    cols = plan.select_columns or plan.group_by or [g.column_id for g in plan.date_groupings]
    if cols:
        return cols[0].split(".", 1)[0]
    return plan.datasets[0] if plan.datasets else next(iter(catalog.datasets))


def plan_list_joins(plan: QueryPlan, catalog: Catalog) -> list[JoinEdge]:
    root = root_dataset(plan, catalog)
    targets = {c.split(".", 1)[0] for c in column_ids_in_plan(plan)} | set(plan.datasets)
    return plan_joins(catalog.graph, root, targets, set(plan.relationship_ids))


def output_names(plan: QueryPlan, measures: list[Measure]) -> list[str]:
    return [*plan.group_by, *[m.alias for m in measures]]


def aggregation_is_numeric_only(agg: Aggregation) -> bool:
    return agg.function in ("sum", "avg")


def raise_if(cond: bool, code: str, message: str, **details) -> None:
    if cond:
        raise AppError(code, message, stage="validate", details=details or None)  # type: ignore[arg-type]
