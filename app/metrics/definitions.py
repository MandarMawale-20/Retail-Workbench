"""Versioned metric registry. Metrics declare required *roles* and multiple binding variants."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DEFINITION_VERSION = "metrics-1.1"
Expr = tuple  # ("ref", t) | ("add"|"sub"|"div"|"pct", a, b)


class TermSpec(BaseModel):
    """One aggregate term; `roles` are multiplied together for `sum`."""
    agg: Literal["sum", "count_distinct", "count_rows", "count_status", "count_zero_or_less", "count_low_stock",
                 "avg_age_days"]
    roles: list[str] = Field(default_factory=list)
    same_dataset_as: str | None = None


class MetricVariant(BaseModel):
    variant_id: str
    required_roles: list[str]
    optional_roles: list[str] = Field(default_factory=list)
    formula_id: str
    requires_relationship_between: list[tuple[str, str]] = Field(default_factory=list)
    terms: dict[str, TermSpec]
    expression: Expr
    description: str


class MetricDefinition(BaseModel):
    metric_id: str
    name: str
    description: str
    unit: Literal["count", "currency", "percent", "ratio"]
    variants: list[MetricVariant]
    null_policy: str = "NULL values are ignored by SUM; rows with NULL keys are kept in their own group"
    definition_version: str = DEFINITION_VERSION
    default_grain: str | None = None
    is_default: bool = False


def _variant(vid: str, terms: dict[str, TermSpec], expr: Expr, description: str, formula_id: str | None = None) -> MetricVariant:
    roles = sorted({r for t in terms.values() for r in t.roles})
    return MetricVariant(variant_id=vid, required_roles=roles, formula_id=formula_id or f"{vid}",
                         terms=terms, expression=expr, description=description)


def _gross_variants(extra: dict[str, TermSpec] | None, expr: Expr, tag: str) -> list[MetricVariant]:
    extra = extra or {}
    return [
        _variant(f"{tag}_line_amount", {"g": TermSpec(agg="sum", roles=["line_amount"]), **extra}, expr,
                 "SUM(line_amount)"),
        _variant(f"{tag}_quantity_x_price", {"g": TermSpec(agg="sum", roles=["quantity", "unit_price"]), **extra}, expr,
                 "SUM(quantity x unit_price)"),
    ]


def build_registry() -> dict[str, MetricDefinition]:
    reg = [
        MetricDefinition(metric_id="units_sold", name="Units sold", description="Total quantity sold", unit="count",
                         variants=[_variant("units_sum", {"n": TermSpec(agg="sum", roles=["quantity"])}, ("ref", "n"),
                                            "SUM(quantity)")]),
        MetricDefinition(metric_id="gross_sales_revenue", name="Gross sales revenue",
                         description="Sales value before returns (default revenue basis)", unit="currency",
                         variants=_gross_variants(None, ("ref", "g"), "gross"), is_default=True),
        MetricDefinition(metric_id="net_sales_revenue", name="Net sales revenue",
                         description="Gross sales revenue minus returned amount", unit="currency",
                         variants=_gross_variants({"r": TermSpec(agg="sum", roles=["return_amount"])}, ("sub", "g", "r"), "net")),
        MetricDefinition(metric_id="order_count", name="Order count", description="Distinct orders", unit="count",
                         variants=[_variant("orders_distinct", {"c": TermSpec(agg="count_distinct", roles=["order_id"])},
                                            ("ref", "c"), "COUNT(DISTINCT order_id)")]),
        MetricDefinition(metric_id="average_order_value", name="Average order value",
                         description="Gross sales revenue divided by distinct orders", unit="currency",
                         variants=_gross_variants({"c": TermSpec(agg="count_distinct", roles=["order_id"])}, ("div", "g", "c"), "aov")),
        MetricDefinition(metric_id="return_rate", name="Return rate",
                         description="Returned units as a percentage of units sold", unit="percent",
                         variants=[
                             _variant("returns_dataset", {"r": TermSpec(agg="sum", roles=["returned_quantity"]),
                                                          "q": TermSpec(agg="sum", roles=["quantity"])}, ("pct", "r", "q"),
                                      "SUM(returned_quantity) / SUM(quantity)"),
                             _variant("status_column", {"r": TermSpec(agg="count_status", roles=["return_status"]),
                                                        "d": TermSpec(agg="count_rows", same_dataset_as="r")}, ("pct", "r", "d"),
                                      "rows with a return status / total rows"),
                         ]),
        MetricDefinition(metric_id="inventory_on_hand", name="Inventory on hand", description="Total available stock",
                         unit="count", variants=[_variant("stock_sum", {"s": TermSpec(agg="sum", roles=["available_stock"])},
                                                          ("ref", "s"), "SUM(available_stock)")]),
        MetricDefinition(metric_id="sell_through", name="Sell-through rate",
                         description="Units sold divided by units sold plus available stock", unit="percent",
                         variants=[_variant("sold_plus_stock", {
                             "q": TermSpec(agg="sum", roles=["quantity"]),
                             "s": TermSpec(agg="sum", roles=["available_stock"]),
                         }, ("pct", "q", ("add", "q", "s")), "100 * units sold / (units sold + available stock)")]),
        MetricDefinition(metric_id="stock_out_rate", name="Stock-out rate",
                         description="Percentage of inventory rows with zero or negative stock", unit="percent",
                         variants=[_variant("non_positive_stock", {
                             "o": TermSpec(agg="count_zero_or_less", roles=["available_stock"]),
                             "d": TermSpec(agg="count_rows", same_dataset_as="o"),
                         }, ("pct", "o", "d"), "100 * rows with available_stock <= 0 / inventory rows")]),
        MetricDefinition(metric_id="inventory_age_days", name="Inventory age",
                         description="Average days since inventory was recorded", unit="ratio",
                         variants=[_variant("inventory_date_age", {
                             "a": TermSpec(agg="avg_age_days", roles=["inventory_date"]),
                         }, ("ref", "a"), "AVG(days since inventory_date)")]),
        MetricDefinition(metric_id="stock_risk_score", name="Stock-risk score",
                         description="Percentage of inventory rows at or below reorder level", unit="percent",
                         variants=[_variant("below_reorder_level", {
                             "r": TermSpec(agg="count_low_stock", roles=["available_stock", "reorder_level"]),
                             "d": TermSpec(agg="count_rows", same_dataset_as="r"),
                         }, ("pct", "r", "d"), "100 * rows at or below reorder level / inventory rows")]),
    ]
    return {m.metric_id: m for m in reg}


_REGISTRY = build_registry()


def get_metric(metric_id: str) -> MetricDefinition | None:
    return _REGISTRY.get(metric_id)


def all_metrics() -> dict[str, MetricDefinition]:
    return dict(_REGISTRY)
