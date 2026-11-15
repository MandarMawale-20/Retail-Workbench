"""Fact generation: every number an answer may state is computed here, by code, with a stable id."""
from __future__ import annotations

from typing import Literal, cast

from app.common import h, sanitize_for_prompt
from app.query.compiler import CompiledQuery
from app.query.schemas import ExecutionResult, Fact, QueryPlan

_PREVIEW = 20


def _unit(u: str) -> str:
    return u if u in ("count", "currency", "percent", "ratio") else "text"


def row_label(row: list, key_count: int) -> str:
    if key_count == 0:
        return "overall"
    parts = ["(unmatched)" if v is None else sanitize_for_prompt(v) for v in row[:key_count]]
    return " / ".join(parts)


def generate_facts(evidence_id: str, plan: QueryPlan, compiled: CompiledQuery, result: ExecutionResult) -> list[Fact]:
    facts: list[Fact] = []

    def add(name: str, key: str, value, unit: str, formula: str, ref: str) -> Fact:
        fact = Fact(fact_id="f_" + h(evidence_id, name, key)[:10], name=name, value=value,
                    unit=cast(Literal["count", "currency", "percent", "ratio", "text"], _unit(unit)),
                    formula_id=formula, source_ref=ref)
        facts.append(fact)
        return fact

    add("rows_considered", "rc", result.rows_considered, "count", "count_rows_after_filters", "evidence")
    add("result_rows", "rr", result.result_row_count, "count", "count_result_rows", "evidence")
    keys = len(compiled.grain)
    if plan.intent in ("list", "distinct", "describe") or not compiled.units:
        return facts
    for i, row in enumerate(result.rows[:_PREVIEW]):
        label = row_label(row, keys)
        for j, unit in enumerate(compiled.units):
            value = row[keys + j]
            if isinstance(value, int | float):
                add(f"{label}|{compiled.columns[keys + j]}", f"{i}:{j}", value, unit,
                    compiled.columns[keys + j], f"row{i}")
    if keys and len(result.rows) >= 2:
        first = compiled.columns[keys]
        vals = [(r, r[keys]) for r in result.rows if isinstance(r[keys], int | float)]
        if len(vals) >= 2:
            (r1, v1), (r2, v2) = vals[0], vals[1]
            unit = compiled.units[0]
            add(f"diff_top2|{first}", "d", round(v1 - v2, 6), unit, "difference", f"{row_label(r1, keys)}-{row_label(r2, keys)}")
            if v2:
                add(f"pct_diff_top2|{first}", "p", round((v1 - v2) / abs(v2) * 100, 6), "percent", "pct_difference", "top2")
            top = max(vals, key=lambda x: x[1])
            low = min(vals, key=lambda x: x[1])
            add(f"max|{first}", "max", top[1], unit, "max", row_label(top[0], keys))
            add(f"min|{first}", "min", low[1], unit, "min", row_label(low[0], keys))
    return facts
