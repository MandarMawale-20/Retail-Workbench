"""Registered deterministic transformations. Each is idempotent: f(f(x)) == f(x)."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from app.profiling.statistics import (
    DateAnalysis,
    collapse_ws,
    format_number,
    parse_number,
    render_date,
)


@dataclass
class TransformResult:
    frame: pd.DataFrame
    rows_affected: int = 0
    failures: list[dict] = field(default_factory=list)


Transform = Callable[[pd.DataFrame, list[str], dict], TransformResult]


def _map_column(series: pd.Series, fn: Callable[[str], str | None]) -> tuple[pd.Series, int]:
    out, changed = [], 0
    for v in series.tolist():
        new = v if v is None else fn(v)
        changed += new != v
        out.append(new)
    return pd.Series(out, index=series.index, dtype=object), changed


def normalize_null_tokens(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    tokens = {t.casefold() for t in params["tokens"]}
    out, total = df.copy(), 0
    for c in columns:
        out[c], n = _map_column(df[c], lambda v: None if v.strip().casefold() in tokens else v)
        total += n
    return TransformResult(out, total)


def trim_whitespace(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    out, total = df.copy(), 0
    for c in columns:
        out[c], n = _map_column(df[c], collapse_ws)
        total += n
    return TransformResult(out, total)


def _canonicalize(series: pd.Series, key: Callable[[str], str]) -> tuple[pd.Series, int]:
    """Collapse variants sharing `key` onto their most frequent spelling (ties: lexicographic)."""
    forms: dict[str, Counter[str]] = defaultdict(Counter)
    for v in series.tolist():
        if v is not None:
            forms[key(v)][v] += 1
    target = {k: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, c in forms.items()}
    return _map_column(series, lambda v: target[key(v)])


def normalize_case(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    out, total = df.copy(), 0
    for c in columns:
        out[c], n = _canonicalize(df[c], lambda v: v.casefold())
        total += n
    return TransformResult(out, total)


def format_sku(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    import re
    out, total = df.copy(), 0
    for c in columns:
        out[c], n = _canonicalize(df[c], lambda v: re.sub(r"[^A-Z0-9]", "", v.upper()))
        total += n
    return TransformResult(out, total)


def normalize_category_alias(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    mapping: dict[str, str] = params["mapping"]
    out, total = df.copy(), 0
    for c in columns:
        out[c], n = _map_column(df[c], lambda v: mapping.get(v, v))
        total += n
    return TransformResult(out, total)


def parse_numeric(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    out, total, failures = df.copy(), 0, []
    for c in columns:
        values: list[str | None] = []
        for i, v in enumerate(df[c].tolist()):
            if v is None:
                values.append(None)
                continue
            parsed = parse_number(v)
            if parsed is None:
                failures.append({"column": c, "row": i, "value": v[:80], "reason": "numeric_parse_failure"})
                values.append(None)
            else:
                values.append(format_number(parsed))
        new = pd.Series(values, index=df.index, dtype=object)
        total += sum(a != b for a, b in zip(new.tolist(), df[c].tolist(), strict=True))
        out[c] = new
    return TransformResult(out, total, failures)


def parse_date_iso(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    analysis = DateAnalysis(params["convention"], 1.0, bool(params.get("has_time")), 0)
    out, total, failures = df.copy(), 0, []
    for c in columns:
        values: list[str | None] = []
        for i, v in enumerate(df[c].tolist()):
            if v is None:
                values.append(None)
                continue
            rendered = render_date(v, analysis)
            if rendered is None:
                failures.append({"column": c, "row": i, "value": v[:80], "reason": "date_parse_failure"})
            values.append(rendered)
        new = pd.Series(values, index=df.index, dtype=object)
        total += sum(a != b for a, b in zip(new.tolist(), df[c].tolist(), strict=True))
        out[c] = new
    return TransformResult(out, total, failures)


def dedup_exact_rows(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    out = df.drop_duplicates(keep="first").reset_index(drop=True)
    return TransformResult(out, len(df) - len(out))


def flag_duplicate_ids(df: pd.DataFrame, columns: list[str], params: dict) -> TransformResult:
    return TransformResult(df.copy(), 0)


REGISTRY: dict[str, Transform] = {
    "normalize_null_tokens": normalize_null_tokens,
    "trim_whitespace": trim_whitespace,
    "normalize_case": normalize_case,
    "normalize_category_alias": normalize_category_alias,
    "parse_numeric": parse_numeric,
    "parse_date_iso": parse_date_iso,
    "format_sku": format_sku,
    "dedup_exact_rows": dedup_exact_rows,
    "flag_duplicate_ids": flag_duplicate_ids,
}

# Fixed execution order (normalise-then-dedup is what makes the cleaner idempotent).
EXECUTION_ORDER = ["normalize_null_tokens", "trim_whitespace", "normalize_case", "format_sku",
                   "normalize_category_alias", "parse_numeric", "parse_date_iso", "dedup_exact_rows",
                   "flag_duplicate_ids"]
