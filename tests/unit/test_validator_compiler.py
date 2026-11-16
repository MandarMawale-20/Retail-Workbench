import pytest
from pydantic import ValidationError

from app.query.compiler import compile_plan
from app.query.executor import distinct_values, execute_query
from app.query.schemas import DateGrouping, DateRangeFilter, Filter, MetricRef, QueryPlan, ToolLog
from app.query.validator import required_evidence, validate_plan
from app.schemas import ExecutionContext


@pytest.fixture
def env(main_env):
    svc, _, catalog = main_env
    ctx = ExecutionContext(owner_id="tester", active_dataset_version_ids=list(catalog.version_ids().values()))
    return svc, catalog, ctx, lambda c, s: distinct_values(catalog, svc.settings, c, s)


def revenue(rel_ids, **kw):
    base = dict(intent="top_n", metrics=[MetricRef(metric_id="gross_sales_revenue")], group_by=["products.category"],
                relationship_ids=[rel_ids[("orders.product_id", "products.product_id")]], limit=5)
    return QueryPlan(**{**base, **kw})


def codes(result):
    return {e.code for e in result.errors}


def test_plan_schema_rejects_unknown_keys_and_limits():
    with pytest.raises(ValidationError):
        QueryPlan(intent="aggregate", sql="DROP TABLE x")
    with pytest.raises(ValidationError):
        QueryPlan(intent="aggregate", limit=10**9)
    with pytest.raises(ValidationError):
        QueryPlan(intent="aggregate", owner_id="x")


def test_plan_schema_defaults_null_or_zero_limit_and_rejects_grouped_stage():
    """Live models emit limit null/0 and a non-existent "grouped" stage; tolerate the former, reject the latter."""
    assert QueryPlan(intent="aggregate", limit=None).limit == 100  # type: ignore[arg-type]
    assert QueryPlan(intent="aggregate", limit=0).limit == 100
    assert QueryPlan(intent="aggregate", limit=5).limit == 5
    with pytest.raises(ValidationError):
        QueryPlan(intent="aggregate", dataset_stage="grouped")  # type: ignore[arg-type]


def test_valid_plan_needs_tool_evidence(env, rel_ids):
    svc, catalog, ctx, values = env
    plan = revenue(rel_ids)
    assert codes(validate_plan(plan, ctx, catalog, ToolLog(), values)) == {"TOOL_EVIDENCE_MISSING"}
    assert validate_plan(plan, ctx, catalog, ToolLog(), values, enforce_tools=False).valid
    assert required_evidence(plan, catalog).preview_join == plan.relationship_ids


def test_validator_error_classes(env, rel_ids):
    _, catalog, ctx, values = env
    nt = dict(enforce_tools=False)
    assert "UNKNOWN_COLUMN" in codes(validate_plan(revenue(rel_ids, group_by=["products.nope"]), ctx, catalog, ToolLog(), values, **nt))
    assert "VALUE_NOT_FOUND" in codes(validate_plan(revenue(rel_ids, filters=[Filter(column_id="products.category", op="eq", value="Tee")]),
                                                    ctx, catalog, ToolLog(), values, **nt))
    assert "TYPE_MISMATCH" in codes(validate_plan(revenue(rel_ids, filters=[Filter(column_id="products.category", op="gt", value=1)]),
                                                  ctx, catalog, ToolLog(), values, **nt))
    assert "AMBIGUOUS_DATE" in codes(validate_plan(revenue(rel_ids, filters=[Filter(column_id="orders.order_date", op="eq", value="2024-01-02")]),
                                                   ctx, catalog, ToolLog(), values, **nt))
    assert "SCOPE_VIOLATION" in codes(validate_plan(revenue(rel_ids, datasets=["other"]), ctx, catalog, ToolLog(), values, **nt))
    assert "UNKNOWN_RELATIONSHIP" in codes(validate_plan(revenue(rel_ids, relationship_ids=["rel_nope"]), ctx, catalog, ToolLog(), values, **nt))
    assert "UNKNOWN_RELATIONSHIP" in codes(validate_plan(revenue(rel_ids, relationship_ids=[]), ctx, catalog, ToolLog(), values, **nt))
    assert "PII_COLUMN" in codes(validate_plan(QueryPlan(intent="list", select_columns=["customers.customer_name"]), ctx, catalog,
                                               ToolLog(), values, **nt))
    bad_ctx = ctx.model_copy(update={"active_dataset_version_ids": ["dv_stale"]})
    assert "SCOPE_VIOLATION" in codes(validate_plan(revenue(rel_ids), bad_ctx, catalog, ToolLog(), values, **nt))


def test_missing_roles_are_unsupported(env):
    _, catalog, ctx, values = env
    # cost data does not exist, so gross margin style plans can only reference registered metrics: units + missing metric id
    plan = QueryPlan(intent="aggregate", metrics=[MetricRef(metric_id="gross_margin")])
    assert "UNSUPPORTED_METRIC" in codes(validate_plan(plan, ctx, catalog, ToolLog(), values, enforce_tools=False))


def test_raw_stage_rules(env):
    _, catalog, ctx, values = env
    sum_raw = QueryPlan(intent="aggregate", dataset_stage="raw", aggregations=[{"function": "sum", "column_id": "orders.quantity", "alias": "s"}])
    assert "STAGE_RULE" in codes(validate_plan(sum_raw, ctx, catalog, ToolLog(), values, enforce_tools=False))


def test_time_bucketing_is_typed_and_clean_stage_only(env):
    _, catalog, ctx, values = env
    plan = QueryPlan(
        intent="aggregate",
        metrics=[MetricRef(metric_id="gross_sales_revenue")],
        date_groupings=[DateGrouping(column_id="orders.order_date", grain="month")],
        date_ranges=[DateRangeFilter(column_id="orders.order_date", start="2024-01-01", end="2024-01-31")],
    )
    result = validate_plan(plan, ctx, catalog, ToolLog(), values, enforce_tools=False)
    assert "AMBIGUOUS_DATE" in codes(result)
    raw = plan.model_copy(update={"dataset_stage": "raw"})
    assert "STAGE_RULE" in codes(validate_plan(raw, ctx, catalog, ToolLog(), values, enforce_tools=False))


def test_compiler_uses_parameters_and_never_select_star(env, rel_ids):
    svc, catalog, ctx, values = env
    plan = revenue(rel_ids, filters=[Filter(column_id="stores.region", op="eq", value="West'; DROP TABLE x;--")],
                   relationship_ids=[rel_ids[("orders.product_id", "products.product_id")], rel_ids[("orders.store_id", "stores.store_id")]])
    compiled = compile_plan(plan, catalog)
    assert "DROP TABLE" not in compiled.sql and "SELECT *" not in compiled.sql.upper()
    assert any("drop table" in str(p).lower() for p in compiled.params)          # value travels as a bound parameter
    result = execute_query(compiled, catalog, svc.settings)
    assert result.result_row_count == 0 and result.rows_considered == 0


def test_executor_is_read_only(main_env):
    import duckdb

    from app.query.executor import open_readonly
    svc, _, catalog = main_env
    con = open_readonly(catalog, svc.settings)
    with pytest.raises(duckdb.Error):
        con.execute("DROP TABLE clean_orders")
    with pytest.raises(duckdb.Error):
        con.execute("SELECT * FROM read_csv('/etc/passwd')")
    con.close()
