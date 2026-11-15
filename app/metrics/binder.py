"""Bind metric variants to concrete columns using inferred roles; missing roles raise UNSUPPORTED_QUERY."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.errors import AppError
from app.metrics.definitions import MetricDefinition, MetricVariant, get_metric
from app.schemas import RoleBinding


class MetricBinding(BaseModel):
    metric_id: str
    variant_id: str
    role_to_column: dict[str, str]
    definition_version: str
    term_datasets: dict[str, str] = Field(default_factory=dict)
    term_columns: dict[str, list[str]] = Field(default_factory=dict)
    binding_uncertain: bool = False
    assumptions: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class RoleCandidate:
    dataset: str
    column_id: str
    confidence: float
    status: str
    rows: int = 0


class RoleIndex:
    """role -> candidates per dataset (verified/candidate only); ambiguous roles are tracked separately."""

    def __init__(self, bindings: list[RoleBinding], rows: dict[str, int] | None = None) -> None:
        self.by_role: dict[str, dict[str, RoleCandidate]] = defaultdict(dict)
        self.ambiguous: dict[str, set[str]] = defaultdict(set)
        for b in bindings:
            ds = b.column_id.split(".", 1)[0]
            if b.status == "ambiguous":
                self.ambiguous[b.role].add(ds)
            elif b.status in ("verified", "candidate"):
                self.by_role[b.role][ds] = RoleCandidate(ds, b.column_id, b.confidence, b.status, (rows or {}).get(ds, 0))

    def datasets_for(self, role: str) -> dict[str, RoleCandidate]:
        return self.by_role.get(role, {})


def _bind_variant(variant: MetricVariant, index: RoleIndex) -> tuple[MetricBinding | None, list[str]]:
    missing: list[str] = []
    term_ds: dict[str, str] = {}
    term_cols: dict[str, list[str]] = {}
    uncertain = False
    assumptions: list[str] = []
    for name, term in variant.terms.items():
        if term.same_dataset_as:
            continue
        if not term.roles:
            continue
        per_role = [set(index.datasets_for(r)) for r in term.roles]
        common = set.intersection(*per_role) if per_role else set()
        if not common:
            missing.extend(r for r, ds in zip(term.roles, per_role, strict=True) if not ds)
            if all(per_role):
                missing.append("+".join(term.roles) + " (no single dataset holds all)")
            continue
        scored = sorted(common, key=lambda d: (-round(min(index.datasets_for(r)[d].confidence for r in term.roles), 1),
                                                          -index.datasets_for(term.roles[0])[d].rows, d))
        chosen = scored[0]
        if len(scored) > 1:
            uncertain = True
            assumptions.append(f"role(s) {term.roles} found in several datasets; used '{chosen}'")
        term_ds[name] = chosen
        term_cols[name] = [index.datasets_for(r)[chosen].column_id for r in term.roles]
        uncertain |= any(index.datasets_for(r)[chosen].status == "candidate" for r in term.roles)
    for name, term in variant.terms.items():
        if term.same_dataset_as and term.same_dataset_as in term_ds:
            term_ds[name], term_cols[name] = term_ds[term.same_dataset_as], []
    if missing or len(term_ds) < len(variant.terms):
        return None, sorted(set(missing))
    role_map = {r: cols[i] for name, cols in term_cols.items()
                for i, r in enumerate(variant.terms[name].roles)}
    return MetricBinding(metric_id="", variant_id=variant.variant_id, role_to_column=role_map, definition_version="",
                         term_datasets=term_ds, term_columns=term_cols, binding_uncertain=uncertain,
                         assumptions=assumptions), []


def bind_metric(metric_id: str, bindings: list[RoleBinding] | RoleIndex) -> MetricBinding:
    metric = get_metric(metric_id)
    if metric is None:
        raise AppError("UNSUPPORTED_QUERY", f"metric '{metric_id}' is not registered", stage="bind",
                       details={"metric_id": metric_id, "missing_roles": []})
    index = bindings if isinstance(bindings, RoleIndex) else RoleIndex(bindings)
    best_missing: list[str] | None = None
    for variant in metric.variants:
        binding, missing = _bind_variant(variant, index)
        if binding:
            binding.metric_id, binding.definition_version = metric_id, metric.definition_version
            binding.assumptions.insert(0, f"{metric.name}: {variant.description}")
            return binding
        if best_missing is None or len(missing) < len(best_missing):
            best_missing = missing
    ambiguous = sorted(r for v in metric.variants for r in v.required_roles if r in index.ambiguous)
    raise AppError("UNSUPPORTED_QUERY", f"cannot compute {metric.name}: missing role(s) {', '.join(best_missing or [])}",
                   stage="bind", details={"metric_id": metric_id, "missing_roles": best_missing or [],
                                          "ambiguous_roles": ambiguous})


def try_bind(metric_id: str, index: RoleIndex) -> MetricBinding | None:
    try:
        return bind_metric(metric_id, index)
    except AppError:
        return None


def metric_definition_summary(metric: MetricDefinition, binding: MetricBinding | None) -> dict:
    return {"metric_id": metric.metric_id, "name": metric.name, "description": metric.description, "unit": metric.unit,
            "definition_version": metric.definition_version,
            "variant": binding.variant_id if binding else None,
            "formula": next((v.description for v in metric.variants if binding and v.variant_id == binding.variant_id),
                            metric.variants[0].description),
            "bound": binding is not None, "binding_uncertain": bool(binding and binding.binding_uncertain)}
