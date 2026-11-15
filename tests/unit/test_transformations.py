import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.cleaning.transformations import (
    dedup_exact_rows,
    normalize_case,
    normalize_category_alias,
    normalize_null_tokens,
    parse_date_iso,
    parse_numeric,
    trim_whitespace,
)
from app.profiling.statistics import analyze_dates, date_token, infer_type, parse_number, render_date

text = st.one_of(st.none(), st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=12))


def frame(values):
    return pd.DataFrame({"a": pd.Series(values, dtype=object)})


def same(x, y):
    return x.equals(y)


@settings(max_examples=60, deadline=None)
@given(st.lists(text, max_size=25))
def test_text_transformations_are_idempotent(values):
    df = frame(values)
    params = {"tokens": ["n/a", "-", "null"]}
    for fn, p in [(normalize_null_tokens, params), (trim_whitespace, {}), (normalize_case, {})]:
        once = fn(df, ["a"], p).frame
        assert same(fn(once, ["a"], p).frame, once)
        assert list(once.columns) == ["a"] and len(once) == len(df)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.one_of(st.none(), st.sampled_from(["1,200.50", "₹300", "12", "1.200,50", "abc", "(5)", " 7 "])), max_size=25))
def test_parse_numeric_idempotent_and_failures_listed(values):
    df = frame(values)
    result = parse_numeric(df, ["a"], {})
    again = parse_numeric(result.frame, ["a"], {})
    assert same(again.frame, result.frame) and not again.failures
    failed_rows = {f["row"] for f in result.failures}
    assert all(result.frame["a"].iloc[i] is None for i in failed_rows)


def test_parse_numeric_values():
    assert parse_number("₹1,200.50") == 1200.5
    assert parse_number("1.200,50") is None          # ambiguous European style is never guessed
    assert parse_number("(5)") == -5


def test_dates_ambiguity_rule():
    assert analyze_dates(["01/02/2024", "03/04/2024"]).kind == "ambiguous"
    assert analyze_dates(["25/03/2024", "01/02/2024"]).kind == "dayfirst"
    assert analyze_dates(["03/25/2024", "01/02/2024"]).kind == "monthfirst"
    assert analyze_dates(["25/03/2024", "03/25/2024"]).kind == "conflicting"


def test_dates_support_timezone_and_epoch_values():
    assert date_token("2024-01-02T03:04:05+05:30") is not None
    analysis = analyze_dates(["2024-01-02T03:04:05+05:30", "2024-01-03T00:00:00Z"])
    assert analysis.kind == "iso" and analysis.has_time
    assert render_date("2024-01-02T03:04:05+05:30", analysis) == "2024-01-01 21:34:05"
    assert infer_type(["1704067200", "1704153600"]) == "date"


def test_parse_date_iso_idempotent():
    df = frame(["25/03/2024", "01/02/2024", None])
    params = {"convention": "dayfirst"}
    once = parse_date_iso(df, ["a"], params).frame
    assert once["a"].tolist() == ["2024-03-25", "2024-02-01", None]
    assert same(parse_date_iso(once, ["a"], {"convention": "iso"}).frame, once)


def test_dedup_and_alias_invariants():
    df = pd.DataFrame({"a": ["x", "x", "y"], "b": ["1", "1", "2"]}, dtype=object)
    result = dedup_exact_rows(df, ["a", "b"], {})
    assert len(result.frame) == len(df) - result.rows_affected == 2
    mapped = normalize_category_alias(frame(["Tee", "Tee", "Shoes"]), ["a"], {"mapping": {"Tee": "T-Shirt"}}).frame
    assert mapped["a"].tolist() == ["T-Shirt", "T-Shirt", "Shoes"]


def test_infer_type():
    assert infer_type(["1", "2", "3"]) == "integer"
    assert infer_type(["1.5", "2"]) == "decimal"
    assert infer_type(["2024-01-01", "2024-02-01"]) == "date"
    assert infer_type(["1", "a", "b"]) == "string"
    assert infer_type([]) == "empty"
