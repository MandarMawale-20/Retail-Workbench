"""Assemble the EvidenceBundle for one executed plan."""
from __future__ import annotations

from app.common import h
from app.evidence.facts import generate_facts
from app.metrics.binder import metric_definition_summary
from app.metrics.definitions import get_metric
from app.query.catalog import Catalog
from app.query.compiler import CompiledQuery
from app.query.schemas import EvidenceBundle, ExecutionResult, Fact, QueryPlan, ValidationResult
from app.schemas import ExecutionContext

REVENUE_METRICS = {"gross_sales_revenue", "average_order_value"}


def plan_id(plan: QueryPlan) -> str:
    return "qp_" + h(plan.model_dump())[:12]


def build_evidence(plan: QueryPlan, validation: ValidationResult, compiled: CompiledQuery, result: ExecutionResult,
                   catalog: Catalog, ctx: ExecutionContext, trace_id: str) -> EvidenceBundle:
    evidence_id = "ev_" + h(plan_id(plan), validation.bound_dataset_versions, compiled.fingerprint)[:12]
    preview = [dict(zip(compiled.columns, row, strict=True)) for row in result.rows[:20]]
    assumptions: list[str] = []
    definitions, bindings, selected = [], [], (
        list(plan.group_by) + list(plan.select_columns) + [g.column_id for g in plan.date_groupings]
        + [r.column_id for r in plan.date_ranges])
    for b in validation.bound_metrics:
        metric = get_metric(b["metric_id"])
        if metric is None:
            continue
        from app.metrics.binder import MetricBinding
        binding = MetricBinding.model_validate(b)
        definitions.append(metric_definition_summary(metric, binding))
        assumptions += [a for a in binding.assumptions if a not in assumptions]
        bindings += [{"metric_id": metric.metric_id, "role": r, "column_id": c} for r, c in sorted(binding.role_to_column.items())]
        selected += [c for cols in binding.term_columns.values() for c in cols]
        if metric.metric_id in REVENUE_METRICS:
            assumptions.append("Revenue basis: gross sales revenue (before returns)")
        if metric.metric_id == "net_sales_revenue":
            assumptions.append("Revenue basis: net sales revenue (gross minus returned amount)")
    for a in plan.aggregations:
        if a.column_id:
            selected.append(a.column_id)
    if plan.dataset_stage == "raw":
        assumptions.append("Raw stage: values are unparsed text; numbers were not cleaned")
    if any(op["op"] == "exclude_null_group_keys" for op in compiled.operations):
        assumptions.append("Ranking excludes rows whose group key is missing or unmatched (NULL)")
    rels = [{"relationship_id": r.relationship_id, "label": r.label(), "cardinality": r.cardinality, "status": r.status}
            for rid in compiled.relationships_used if (r := catalog.graph.get(rid))]
    facts = generate_facts(evidence_id, plan, compiled, result)
    for relationship in rels:
        graph_relationship = catalog.graph.get(relationship["relationship_id"])
        if graph_relationship is not None and graph_relationship.unmatched_left:
            facts.append(Fact(
                fact_id="f_" + h(evidence_id, relationship["relationship_id"], "unmatched")[:10],
                name=f"unmatched_rows|{relationship['label']}",
                value=graph_relationship.unmatched_left,
                unit="count",
                formula_id="relationship_unmatched_left",
                source_ref=relationship["relationship_id"],
            ))
            assumptions.append(
                f"{graph_relationship.unmatched_left} left-side row(s) did not match relationship "
                f"{relationship['label']}; grouped results preserve them as (unmatched) where applicable"
            )
    return EvidenceBundle(
        evidence_id=evidence_id, trace_id=trace_id, dataset_versions=validation.bound_dataset_versions,
        dataset_stage=plan.dataset_stage, graph_version=catalog.graph.graph_version, relationships=rels,
        metric_definitions=definitions, role_bindings=bindings, selected_columns=sorted(set(selected)),
        filters=[f.model_dump() for f in plan.filters] + [r.model_dump() for r in plan.date_ranges],
        operations=compiled.operations, aggregation_grain=compiled.grain,
        null_policy=next((metric.null_policy for b in validation.bound_metrics
                          if (metric := get_metric(b["metric_id"])) is not None), None),
        rows_considered=result.rows_considered, result_row_count=result.result_row_count, result_preview=preview,
        facts=facts, assumptions=assumptions, sql_fingerprint=compiled.fingerprint)
