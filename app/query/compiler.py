"""Compile a validated QueryPlan into parameterised DuckDB SQL. Identifiers come only from the catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.common import h
from app.query.analysis import (
    Measure,
    TermUse,
    build_measures,
    dimension_datasets,
    fact_datasets,
    plan_fact_joins,
    plan_list_joins,
    root_dataset,
)
from app.query.catalog import Catalog
from app.query.schemas import Filter, QueryPlan
from app.relationships.join_policy import JoinEdge


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    columns: list[str]
    units: list[str]
    count_sql: str
    count_params: list[Any]
    fingerprint: str
    limit: int
    relationships_used: list[str] = field(default_factory=list)
    operations: list[dict] = field(default_factory=list)
    grain: list[str] = field(default_factory=list)


def _alias(dataset: str) -> str:
    return f"t_{dataset}"


def _col(catalog: Catalog, column_id: str) -> str:
    return f"{_alias(column_id.split('.', 1)[0])}.{catalog.physical(column_id)}"


def _number(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return value


def filter_sql(catalog: Catalog, f: Filter, stage: str) -> tuple[str, list[Any]]:
    info = catalog.column(f.column_id)
    assert info is not None
    col = _col(catalog, f.column_id)
    typed = info.is_numeric or info.is_date
    text = stage == "raw" or info.is_text
    if f.op == "is_null":
        return f"{col} IS NULL", []
    if f.op == "not_null":
        return f"{col} IS NOT NULL", []
    if f.value is None:
        # No filter value provided; return empty params so the operator
        # effectively becomes a no-op (caller should handle this).
        return "", []
    values = f.value if isinstance(f.value, list) else [f.value]
    if f.op == "contains":
        return f"CONTAINS(LOWER({col}), ?)", [str(values[0]).lower()]
    if text and f.op in ("eq", "neq", "in"):
        marks = ", ".join("?" for _ in values)
        norm = [str(v).strip().lower() for v in values]
        expr = f"LOWER(TRIM({col})) IN ({marks})"
        return (f"NOT ({expr})" if f.op == "neq" else expr), norm
    if info.is_date:
        cast = lambda: f"CAST(? AS {info.sql_type})"  # noqa: E731
        params = [str(v) for v in values]
        if f.op == "between":
            return f"{col} BETWEEN {cast()} AND {cast()}", params[:2]
        op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[f.op]
        return f"{col} {op} {cast()}", params[:1]
    assert typed
    nums = [_number(v) for v in values]
    if f.op == "between":
        return f"{col} BETWEEN ? AND ?", nums[:2]
    if f.op == "in":
        return f"{col} IN ({', '.join('?' for _ in nums)})", nums
    op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[f.op]
    return f"{col} {op} ?", nums[:1]


def _from_clause(catalog: Catalog, root: str, joins: list[JoinEdge], stage: str) -> str:
    sql = f"FROM {catalog.table(root, stage)} AS {_alias(root)}"
    for j in joins:
        sql += (f" LEFT JOIN {catalog.table(j.to_dataset, stage)} AS {_alias(j.to_dataset)}"
                f" ON {_col(catalog, j.from_column)} = {_col(catalog, j.to_column)}")
    return sql


def _where(catalog: Catalog, plan: QueryPlan, datasets: set[str]) -> tuple[str, list[Any]]:
    clauses, params = [], []
    for f in plan.filters:
        if f.column_id.split(".", 1)[0] in datasets:
            sql, p = filter_sql(catalog, f, plan.dataset_stage)
            clauses.append(sql)
            params.extend(p)
    for r in plan.date_ranges:
        if r.column_id.split(".", 1)[0] in datasets:
            col = _col(catalog, r.column_id)
            info = catalog.column(r.column_id)
            assert info is not None
            cast = f"CAST(? AS {info.sql_type})"
            clauses.append(f"{col} BETWEEN {cast} AND {cast}")
            params.extend([r.start.isoformat(), r.end.isoformat()])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _term_sql(catalog: Catalog, t: TermUse) -> tuple[str, list[Any]]:
    cols = [_col(catalog, c) for c in t.columns]
    if t.agg == "sum":
        return "SUM(" + " * ".join(f"CAST({c} AS DOUBLE)" for c in cols) + ")", []
    if t.agg == "avg":
        return f"AVG(CAST({cols[0]} AS DOUBLE))", []
    if t.agg in ("min", "max"):
        return f"{t.agg.upper()}({cols[0]})", []
    if t.agg == "count_distinct":
        return f"COUNT(DISTINCT {cols[0]})", []
    if t.agg == "count":
        return f"COUNT({cols[0]})", []
    if t.agg == "count_rows":
        return "COUNT(*)", []
    if t.agg == "count_zero_or_less":
        return f"SUM(CASE WHEN CAST({cols[0]} AS DOUBLE) <= 0 THEN 1 ELSE 0 END)", []
    if t.agg == "count_low_stock":
        return f"SUM(CASE WHEN CAST({cols[0]} AS DOUBLE) <= CAST({cols[1]} AS DOUBLE) THEN 1 ELSE 0 END)", []
    if t.agg == "avg_age_days":
        return f"AVG(DATE_DIFF('day', CAST({cols[0]} AS DATE), CURRENT_DATE))", []
    marks = ", ".join("?" for _ in t.status_values)
    return f"SUM(CASE WHEN LOWER(TRIM(CAST({cols[0]} AS VARCHAR))) IN ({marks}) THEN 1 ELSE 0 END)", list(t.status_values)


def _expr(measure: Measure, ref) -> str:
    """Compose a metric expression from CTE term columns (NULL terms count as 0; division is NULL-safe)."""
    def term(value) -> str:
        if isinstance(value, tuple):
            op = value[0]
            a, b = term(value[1]), term(value[2])
            if op == "add":
                return f"({a} + {b})"
            if op == "sub":
                return f"({a} - {b})"
            if op == "div":
                return f"({a} / NULLIF({b}, 0))"
            if op == "pct":
                return f"(100.0 * {a} / NULLIF({b}, 0))"
            raise ValueError(f"unsupported metric expression: {op}")
        return f"COALESCE({ref(measure, value)}, 0)" if measure.kind == "metric" else ref(measure, value)

    op = measure.expression
    if op[0] == "ref":
        return term(op[1])
    return term(op)


def compile_plan(plan: QueryPlan, catalog: Catalog) -> CompiledQuery:
    if plan.intent in ("list", "distinct"):
        return _compile_list(plan, catalog, distinct=plan.intent == "distinct")
    if plan.intent == "describe":
        return _compile_describe(plan, catalog)
    return _compile_measures(plan, catalog)


def _order_by(plan: QueryPlan, names: list[str], measures: list[Measure], keys: int) -> str:
    resolve = {n: i + 1 for i, n in enumerate(names)}
    for i, m in enumerate(measures):
        if m.metric_id:
            resolve.setdefault(m.metric_id, keys + i + 1)
    parts = [f"{resolve[s.field]} {s.direction.upper()} NULLS LAST" for s in plan.sort if s.field in resolve]
    if not parts and measures and plan.intent in ("top_n", "aggregate", "compare") and keys:
        parts = [f"{keys + 1} DESC NULLS LAST"]
    parts += [f"{i} ASC NULLS LAST" for i in range(1, keys + 1)]
    return (" ORDER BY " + ", ".join(parts)) if parts else ""


def _compile_measures(plan: QueryPlan, catalog: Catalog) -> CompiledQuery:
    stage = plan.dataset_stage
    measures = build_measures(plan, catalog)
    facts = fact_datasets(measures)
    joins = plan_fact_joins(plan, catalog, measures) if stage == "clean" else {f: [] for f in facts}
    cte_sql: list[str] = []
    params: list[Any] = []
    cte_of = {fact: f"f{i}" for i, fact in enumerate(facts)}
    keys = len(plan.group_by) + len(plan.date_groupings)
    for fact in facts:
        select = [f"{_col(catalog, g)} AS k{j}" for j, g in enumerate(plan.group_by)]
        select += [f"DATE_TRUNC('{g.grain}', {_col(catalog, g.column_id)}) AS k{len(plan.group_by) + j}"
                   for j, g in enumerate(plan.date_groupings)]
        sel_params: list[Any] = []
        for m in measures:
            for name, t in m.terms.items():
                if t.dataset != fact:
                    continue
                sql, p = _term_sql(catalog, t)
                select.append(f"{sql} AS m{m.terms[name].measure_index}_{name}")
                sel_params.extend(p)
        datasets = {fact} | {j.to_dataset for j in joins[fact]}
        where, where_params = _where(catalog, plan, datasets)
        group = (" GROUP BY " + ", ".join(str(i + 1) for i in range(keys))) if keys else ""
        cte_sql.append(f"{cte_of[fact]} AS (SELECT {', '.join(select)} {_from_clause(catalog, fact, joins[fact], stage)}"
                       f"{where}{group})")
        params.extend(sel_params + where_params)
    base = cte_of[facts[0]]

    def ref(measure: Measure, name: str) -> str:
        t = measure.terms[name]
        return f"{cte_of[t.dataset]}.m{t.measure_index}_{name}"

    key_exprs = [f"{base}.k{j}" for j in range(keys)]
    joined = f" FROM {base}"
    for fact in facts[1:]:
        cte = cte_of[fact]
        on = " AND ".join(f"{key_exprs[j]} IS NOT DISTINCT FROM {cte}.k{j}" for j in range(keys)) or "TRUE"
        joined += f" FULL OUTER JOIN {cte} ON {on}"
        key_exprs = [f"COALESCE({key_exprs[j]}, {cte}.k{j})" for j in range(keys)]
    select = [f"{key_exprs[j]} AS c{j}" for j in range(keys)]
    select += [f"{_expr(m, ref)} AS c{keys + i}" for i, m in enumerate(measures)]
    names = [*plan.group_by, *[f"{g.column_id}__{g.grain}" for g in plan.date_groupings],
             *[m.alias for m in measures]]
    order = _order_by(plan, names, measures, keys)
    rank_only = plan.intent == "top_n" and keys > 0
    null_filter = (" WHERE " + " AND ".join(f"{k} IS NOT NULL" for k in key_exprs)) if rank_only else ""
    sql = f"WITH {', '.join(cte_sql)} SELECT {', '.join(select)}{joined}{null_filter}{order} LIMIT {plan.limit + 1}"
    primary = facts[0]
    where, wparams = _where(catalog, plan, {primary} | {j.to_dataset for j in joins[primary]})
    count_sql = f"SELECT COUNT(*) {_from_clause(catalog, primary, joins[primary], stage)}{where}"
    used = sorted({j.relationship_id for js in joins.values() for j in js})
    ops: list[dict[str, Any]] = [{"op": "rows_considered_from", "dataset": primary}]
    ops += [{"op": "date_group", "column": g.column_id, "grain": g.grain} for g in plan.date_groupings]
    ops += [{"op": "date_range", "column": r.column_id, "start": r.start.isoformat(), "end": r.end.isoformat()}
            for r in plan.date_ranges]
    if rank_only:
        ops.append({"op": "exclude_null_group_keys",
                    "columns": list(plan.group_by) + [g.column_id for g in plan.date_groupings]})
    ops += [{"op": "aggregate", "measure": m.alias, "metric_id": m.metric_id,
            "variant": m.binding.variant_id if m.binding else None,
            "terms": {n: {"dataset": t.dataset, "agg": t.agg, "columns": t.columns} for n, t in sorted(m.terms.items())}}
           for m in measures]
    return CompiledQuery(sql, params, names, [m.unit for m in measures], count_sql, wparams, _fingerprint(sql, params),
                         plan.limit, used, ops,
                         list(plan.group_by) + [f"{g.column_id}__{g.grain}" for g in plan.date_groupings])


def _select_list(plan: QueryPlan, catalog: Catalog) -> list[str]:
    return list(plan.select_columns)


def _compile_list(plan: QueryPlan, catalog: Catalog, *, distinct: bool) -> CompiledQuery:
    stage = plan.dataset_stage
    root = root_dataset(plan, catalog)
    joins = plan_list_joins(plan, catalog) if stage == "clean" else []
    datasets = {root} | {j.to_dataset for j in joins}
    cols = _select_list(plan, catalog)
    select = ", ".join(f"{_col(catalog, c)} AS c{i}" for i, c in enumerate(cols))
    where, params = _where(catalog, plan, datasets)
    order_parts = [f"{cols.index(s.field) + 1} {s.direction.upper()} NULLS LAST" for s in plan.sort if s.field in cols]
    order = " ORDER BY " + ", ".join(order_parts or ["1 ASC NULLS LAST"])
    frm = _from_clause(catalog, root, joins, stage)
    sql = f"SELECT {'DISTINCT ' if distinct else ''}{select} {frm}{where}{order} LIMIT {plan.limit + 1}"
    count_sql = f"SELECT COUNT(*) {frm}{where}"
    op = {"op": "distinct" if distinct else "list", "columns": cols}
    return CompiledQuery(sql, params, cols, ["text"] * len(cols), count_sql, list(params), _fingerprint(sql, params),
                         plan.limit, sorted(j.relationship_id for j in joins), [op], [])


def _compile_describe(plan: QueryPlan, catalog: Catalog) -> CompiledQuery:
    root = root_dataset(plan, catalog)
    cols = plan.select_columns or [c.column_id for c in catalog.datasets[root].columns.values()][:40]
    joins = plan_list_joins(plan.model_copy(update={"select_columns": cols}), catalog) if plan.dataset_stage == "clean" else []
    datasets = {root} | {j.to_dataset for j in joins}
    where, wparams = _where(catalog, plan, datasets)
    frm = _from_clause(catalog, root, joins, plan.dataset_stage)
    parts, params = [], []
    for c in cols:
        info = catalog.column(c)
        col = _col(catalog, c)
        avg = f"AVG(CAST({col} AS DOUBLE))" if info and info.is_numeric else "CAST(NULL AS DOUBLE)"
        parts.append(f"SELECT ? AS c0, COUNT(*) AS c1, COUNT({col}) AS c2, COUNT(DISTINCT {col}) AS c3, "
                     f"CAST(MIN({col}) AS VARCHAR) AS c4, CAST(MAX({col}) AS VARCHAR) AS c5, {avg} AS c6 {frm}{where}")
        params.extend([c, *wparams])
    sql = " UNION ALL ".join(parts) + f" ORDER BY 1 LIMIT {plan.limit + 1}"
    names = ["column", "rows", "non_null", "distinct", "min", "max", "avg"]
    return CompiledQuery(sql, params, names, ["text", "count", "count", "count", "text", "text", "ratio"],
                         f"SELECT COUNT(*) {frm}{where}", list(wparams), _fingerprint(sql, params), plan.limit,
                         sorted(j.relationship_id for j in joins), [{"op": "describe", "columns": cols}], [])


def _fingerprint(sql: str, params: list[Any]) -> str:
    return "sq_" + h(sql, [type(p).__name__ for p in params])[:12]


__all__ = ["CompiledQuery", "compile_plan", "dimension_datasets", "filter_sql"]
