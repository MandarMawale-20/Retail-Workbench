import json

import pytest

from app.llm.base import MeteredProvider
from app.llm.mock_provider import MockProvider
from app.services.chat import ask, ask_with_stats, create_session
from tests.conftest import OWNER


def fact(result, label, metric):
    return next(f.value for f in result.evidence.facts if f.name.split("|")[0] == label and metric in f.name)


def test_top_categories_match_pandas_oracle(session, oracle):
    svc, sid = session
    res = ask(svc, OWNER, sid, "Top 5 categories by revenue")
    assert res.status == "ok" and res.evidence.result_row_count == 5
    for category, value in oracle.revenue_by_category().head(5).items():
        assert fact(res, category, "gross_sales_revenue") == pytest.approx(value, rel=1e-6)
    ev = res.evidence
    assert ev.rows_considered == len(oracle.orders) and ev.dataset_stage == "clean" and ev.relationships
    assert any("Revenue basis: gross" in a for a in ev.assumptions)
    assert "Revenue basis" in res.answer_text and ev.evidence_id in res.answer_text
    assert len(ev.result_preview) <= 20 and "plan" in res.model_dump()


def test_multi_turn_filter_then_followup_matches_oracle(session, oracle):
    svc, sid = session
    ask(svc, OWNER, sid, "Top 5 categories by revenue")
    west = ask(svc, OWNER, sid, "only stores in the West region")
    assert west.status == "ok" and [f.column_id for f in west.plan.filters] == ["stores.region"]
    for category, value in oracle.revenue_by_category("West").head(3).items():
        assert fact(west, category, "gross_sales_revenue") == pytest.approx(value, rel=1e-6)
    rr = ask(svc, OWNER, sid, "among those, which category has the highest return rate?")
    top = oracle.return_rate_by_category("West")
    assert rr.status == "ok" and rr.plan.filters == west.plan.filters and rr.evidence.result_row_count == 1
    assert [m.metric_id for m in rr.plan.metrics] == ["return_rate"]
    assert rr.plan.group_by == ["products.category"]
    # The follow-up keeps the West filter and carries the join/evidence chain the answer depends on.
    assert any(f.get("value") == "West" for f in rr.evidence.filters)
    assert rr.evidence.relationships and rr.evidence.dataset_stage == "clean"
    assert fact(rr, top.index[0], "return_rate") == pytest.approx(top.iloc[0], rel=1e-6)
    stock = ask(svc, OWNER, sid, "among those, which is lowest on stock?")
    assert stock.status == "ok" and stock.plan.filters == west.plan.filters
    assert fact(stock, oracle.stock_by_category("West").index[0], "inventory_on_hand") == oracle.stock_by_category("West").iloc[0]


def test_unrelated_topic_resets_filters(session):
    svc, sid = session
    ask(svc, OWNER, sid, "Top 5 categories by revenue")
    ask(svc, OWNER, sid, "only stores in the West region")
    fresh = ask(svc, OWNER, sid, "total units sold")
    assert fresh.status == "ok" and fresh.plan.filters == []


def test_multi_fact_ranking_keeps_zero_return_categories(session, oracle):
    svc, sid = session
    res = ask(svc, OWNER, sid, "Which categories have highest return rate and are low on stock?")
    expected = oracle.return_rate_by_category()
    assert res.status == "ok" and res.evidence.result_row_count == len(expected)
    for category, rate in expected.items():
        assert fact(res, category, "return_rate") == pytest.approx(rate, rel=1e-6)


def test_store_comparison_and_out_of_stock_flows(session, oracle):
    svc, sid = session
    comparison = ask(svc, OWNER, sid, "compare West and East stores on return rate")
    assert comparison.status == "ok"
    assert comparison.plan.intent == "compare"
    assert comparison.plan.group_by == ["stores.region"]
    assert set(comparison.plan.filters[0].value) == {"West", "East"}

    out = ask(svc, OWNER, sid, "which products are out of stock in S03?")
    assert out.status == "ok"
    assert out.plan.intent == "list"
    assert out.plan.filters[-1].op == "lte" and out.plan.filters[-1].value == 0
    assert out.plan.filters[0].value == "S03"


def test_refusals_and_clarifications(session):
    svc, sid = session
    margin = ask(svc, OWNER, sid, "What is our profit margin?")
    assert margin.status == "unsupported" and margin.error["details"]["missing_roles"] == ["unit_cost"] and margin.evidence is None
    rank = ask(svc, OWNER, sid, "Which products performed best?")
    assert rank.status == "clarification_required" and set(rank.clarification["options"]) == {"Units sold", "Gross sales revenue"}
    answered = ask(svc, OWNER, sid, "units sold")                       # answers the pending clarification
    assert answered.status == "ok" and answered.plan.metrics[0].metric_id == "units_sold"
    tee = ask(svc, OWNER, sid, "Sales in Tee category")
    assert tee.status == "clarification_required" and "T-Shirt" in tee.answer_text and tee.evidence is None
    date = ask(svc, OWNER, sid, "Orders in January")
    assert date.status == "clarification_required" and date.clarification["kind"] == "date_format"


def test_raw_vs_clean_row_counts(session, oracle):
    svc, sid = session
    raw = ask(svc, OWNER, sid, "how many rows in orders raw")
    clean = ask(svc, OWNER, sid, "how many rows in orders")
    assert fact(raw, "overall", "row_count") == oracle.orders_all_rows and raw.evidence.dataset_stage == "raw"
    assert fact(clean, "overall", "row_count") == len(oracle.orders) and clean.evidence.dataset_stage == "clean"
    assert raw.evidence.dataset_versions == clean.evidence.dataset_versions


def test_every_number_in_answer_is_a_fact(session):
    from app.evaluation.compare import answer_is_grounded
    svc, sid = session
    for q in ["Top 5 categories by revenue", "gross vs net revenue", "avg unit price", "count of orders"]:
        res = ask(svc, OWNER, sid, q)
        assert res.status == "ok" and answer_is_grounded(res), q


def test_unsafe_join_is_refused_not_answered(data_dir, tmp_path):
    from app.schemas import PipelineOptions
    from app.services.context import Services
    from app.services.pipeline import run_pipeline
    from tests.conftest import make_files
    svc = Services.create(artifacts_dir=tmp_path)
    run = run_pipeline(OWNER, make_files(data_dir, ["products_dup", "orders", "stores"]), PipelineOptions(), svc)
    assert any(i["issue_type"] == "duplicate_ids" for i in run["issues"])
    assert any(r["status"] == "unsafe" for r in run["graph"]["relationships"])
    res = ask(svc, OWNER, create_session(svc, OWNER, run["run_id"]), "Revenue by category")
    assert res.status == "refused" and res.error["code"] == "UNSAFE_JOIN" and res.evidence is None


def test_session_isolation_and_idempotency(session):
    from app.errors import AppError
    svc, sid = session
    with pytest.raises(AppError) as exc:
        ask(svc, "intruder", sid, "total units sold")
    assert exc.value.code == "ISOLATION_VIOLATION"
    with pytest.raises(AppError):
        svc.repo.get_evidence("intruder", ask(svc, OWNER, sid, "total units sold").evidence.evidence_id)
    first = ask(svc, OWNER, sid, "total units sold", idem_key="k1")
    again = ask(svc, OWNER, sid, "total units sold", idem_key="k1")
    assert first.turn_id == again.turn_id and len(svc.repo.list_turns(OWNER, sid)) == len([t for t in svc.repo.list_turns(OWNER, sid)])


def test_turns_persist_full_chain(session):
    svc, sid = session
    res = ask(svc, OWNER, sid, "Top 5 categories by revenue")
    turn = svc.repo.list_turns(OWNER, sid)[-1]
    assert turn["plan"] and turn["validation"]["valid"] and turn["execution"]["rows_considered"] and turn["answer"]["answer_text"]
    trace = svc.repo.get_trace(OWNER, res.trace_id)
    assert [e["component"] for e in trace if e["component"] in ("planner", "validation", "execute", "facts", "final")] == \
        ["planner", "validation", "execute", "facts", "final"]
    assert "system prompt" not in json.dumps(trace).lower()


# ---------------------------------------------------------------- LLM planner loop (scripted provider)
def llm(responses):
    return MeteredProvider(MockProvider(responses))


def plan_json(rel_ids, **extra):
    plan = {"intent": "top_n", "metrics": [{"metric_id": "gross_sales_revenue"}], "group_by": ["products.category"], "limit": 5,
            "relationship_ids": [rel_ids[("orders.product_id", "products.product_id")]], **extra}
    return json.dumps({"type": "final_plan", "plan": plan})


def tool_call(rel_ids):
    return json.dumps({"type": "tool_call", "calls": [{"tool": "preview_join", "args": {
        "relationship_id": rel_ids[("orders.product_id", "products.product_id")]}}]})


def test_llm_inspect_then_plan_is_tool_compliant(session, rel_ids, oracle):
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue", provider=llm([tool_call(rel_ids), plan_json(rel_ids)]))
    assert out.result.status == "ok" and out.stats.tool_compliant and not out.stats.repaired and out.stats.llm_calls >= 2
    assert [c.source for c in out.tool_log.calls] == ["llm_requested"]
    assert fact(out.result, oracle.revenue_by_category().index[0], "gross_sales_revenue") == pytest.approx(oracle.revenue_by_category().iloc[0])


def test_llm_skipping_tools_costs_the_repair_and_code_injects_results(session, rel_ids):
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue", provider=llm([plan_json(rel_ids), plan_json(rel_ids)]))
    assert out.result.status == "ok" and out.stats.repaired and not out.stats.tool_compliant
    assert [c.source for c in out.tool_log.calls] == ["code_injected"]


def test_invalid_json_is_repaired_once_then_falls_back(session):
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "total units sold", provider=llm(["nope", "still nope"]))
    assert out.result.status == "ok" and out.stats.fallback_used and out.stats.invalid_json


@pytest.mark.parametrize("script", [["SIMULATE:timeout"], ["SIMULATE:rate_limited"], []])
def test_llm_outage_degrades_to_fallback(session, script):
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "total units sold", provider=llm(script))
    assert out.result.status == "ok" and out.stats.fallback_used and out.stats.planner_source == "fallback"
    assert "units sold" in out.result.answer_text


def test_llm_plan_with_hallucinated_column_is_refused_after_one_repair(session, rel_ids):
    svc, sid = session
    bad = plan_json(rel_ids, group_by=["products.colour"])
    out = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue", provider=llm([tool_call(rel_ids), bad, bad]))
    assert out.result.status == "refused" and out.stats.repaired and out.result.evidence is None


def test_tool_evidence_after_parse_repair_prefers_valid_fallback(session, rel_ids, oracle):
    """Once the single repair is spent on parse errors, a plan lacking tool evidence falls back instead of refusing."""
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue",
                         provider=llm(["{not valid json", plan_json(rel_ids)]), use_narrator=False)
    assert out.result.status == "ok" and out.stats.fallback_used and out.stats.planner_source == "fallback"
    assert any(e["component"] == "planner_override" and e["original"] == "tool_evidence_missing" for e in out.trace)
    top = oracle.revenue_by_category()
    assert fact(out.result, top.index[0], "gross_sales_revenue") == pytest.approx(top.iloc[0], rel=1e-6)


def test_narrator_accepted_and_rejected_paths(session, rel_ids):
    svc, sid = session
    def segs(*s):
        return json.dumps({"segments": list(s)})

    base = [tool_call(rel_ids), plan_json(rel_ids)]
    good = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue", provider=llm([*base, segs({"type": "text", "text": "Done."})]),
                          use_narrator=True)
    assert good.result.answer_source == "narrator" and "Revenue basis" in good.result.answer_text
    bad = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue",
                         provider=llm([*base, segs({"type": "text", "text": "Revenue was 12345 in total"})]), use_narrator=True)
    assert bad.result.answer_source == "template" and bad.stats.narrator_attempted and not bad.stats.narrator_accepted
    off = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue", provider=llm(base), use_narrator=False)
    assert off.result.answer_source == "template" and not off.stats.narrator_attempted


def test_context_budget_trims_then_refuses(session):
    from pathlib import Path

    from app.config import Settings
    from app.conversation.linker import build_schema_context
    from app.errors import AppError
    from app.query.catalog import Catalog
    from app.query.schemas import ConversationState
    svc, sid = session
    catalog = Catalog.load(svc.store.run_dir(OWNER, svc.repo.get_session(OWNER, sid)["run_id"]))
    state = ConversationState(session_id=sid, owner_id=OWNER)
    full = build_schema_context("revenue by category", catalog, state, max_tokens=6000)
    assert not full.trim_log and len(full.columns) <= 40
    trimmed = build_schema_context("revenue by category", catalog, state, max_tokens=2300)
    assert trimmed.trim_log[0] == "dropped lowest-scoring column" and len(trimmed.columns) < len(full.columns)
    tighter = build_schema_context("revenue by category", catalog, state, max_tokens=1900)
    assert tighter.trim_log == ["dropped lowest-scoring column", "list_distinct limit 50->20", "dropped metric descriptions"]
    with pytest.raises(AppError) as exc:
        build_schema_context("revenue by category", catalog, state, max_tokens=1800)
    assert exc.value.code == "UNSUPPORTED_QUERY"
    _ = (Settings, Path)


# ------------------------------------------------ documented-default guard against over-conservative LLM abstention
AGNES_REVENUE_CLARIFY = json.dumps({
    "type": "clarify",
    "question": "Which definition of revenue should I use for the ranking: gross sales revenue or net sales revenue?",
    "options": ["gross_sales_revenue (default)", "net_sales_revenue"],
})


def test_revenue_clarification_is_overridden_by_documented_default(session, oracle):
    """A live model over-clarified gross-vs-net; the documented default must win without losing the turn."""
    svc, sid = session
    out = ask_with_stats(svc, OWNER, sid, "Top 5 categories by revenue",
                         provider=llm([AGNES_REVENUE_CLARIFY]), use_narrator=False)
    res = out.result
    assert res.status == "ok" and res.plan is not None
    assert [m.metric_id for m in res.plan.metrics] == ["gross_sales_revenue"]
    assert res.evidence is not None and res.evidence.result_row_count == 5
    assert out.stats.fallback_used and out.stats.planner_source == "fallback"
    assert any(e["component"] == "planner_override" for e in out.trace)
    top = oracle.revenue_by_category()
    assert fact(res, top.index[0], "gross_sales_revenue") == pytest.approx(top.iloc[0], rel=1e-6)


def test_followup_refusal_is_overridden_by_rule_delta(session):
    """An LLM 'unanswerable' for a filter-only follow-up must become a delta against the previous plan."""
    svc, sid = session
    ask(svc, OWNER, sid, "Top 5 categories by revenue")
    refusal = json.dumps({"type": "unanswerable", "reason": "the request is too vague", "missing": []})
    out = ask_with_stats(svc, OWNER, sid, "only stores in the West region", provider=llm([refusal]), use_narrator=False)
    assert out.result.status == "ok" and out.result.plan is not None
    assert [f.value for f in out.result.plan.filters] == ["West"]
    assert out.stats.fallback_used and any(e["component"] == "planner_override" for e in out.trace)


def test_genuine_ambiguity_and_missing_data_keep_their_abstention(session):
    svc, sid = session
    ambiguous = json.dumps({"type": "clarify", "question": "Rank by units sold or revenue?",
                            "options": ["Units sold", "Gross sales revenue"]})
    out = ask_with_stats(svc, OWNER, sid, "Which products performed best?", provider=llm([ambiguous]), use_narrator=False)
    assert out.result.status == "clarification_required" and not out.stats.fallback_used
    refusal = json.dumps({"type": "unanswerable", "reason": "no cost data", "missing": ["unit_cost"]})
    out = ask_with_stats(svc, OWNER, sid, "What is our profit margin?", provider=llm([refusal]), use_narrator=False)
    assert out.result.status == "unsupported" and out.result.error["details"]["missing_roles"] == ["unit_cost"]

