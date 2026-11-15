"""Dataset and column profiling over all-string frames."""
from __future__ import annotations

from collections import Counter
from statistics import mean, pstdev
from typing import Literal, cast

import pandas as pd

from app.common import h, sanitize_for_prompt
from app.profiling.statistics import (
    analyze_dates,
    infer_type,
    non_null_values,
    null_tokens,
    parse_number,
)
from app.schemas import ColumnProfile, DatasetProfile


def _quantile(sorted_vals: list[float], q: float) -> float:
    idx = q * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def profile_column(series: pd.Series, name: str, tokens: set[str]) -> ColumnProfile:
    total = len(series)
    values = non_null_values(series, tokens)
    inferred = infer_type(values)
    counts = Counter(values)
    parse_failures: dict[str, int] = {}
    numeric_stats = None
    min_v = max_v = None
    if inferred in ("integer", "decimal", "mixed"):
        nums = sorted(p for p in (parse_number(v) for v in values) if p is not None)
        parse_failures["numeric"] = len(values) - len(nums)
        if nums:
            numeric_stats = {"mean": round(mean(nums), 6), "std": round(pstdev(nums), 6),
                             "p25": _quantile(nums, 0.25), "p50": _quantile(nums, 0.5), "p75": _quantile(nums, 0.75)}
            if inferred != "mixed":
                min_v, max_v = str(nums[0]), str(nums[-1])
    if inferred == "date":
        parse_failures["date"] = analyze_dates(values).failures
        min_v, max_v = (min(values), max(values)) if values else (None, None)
    elif inferred in ("string", "boolean") and values:
        min_v, max_v = min(values), max(values)
    top = [(sanitize_for_prompt(v), n) for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
    return ColumnProfile(
        name=name, inferred_type=inferred, null_count=total - len(values),
        null_pct=round((total - len(values)) / total, 6) if total else 0.0,
        distinct_count=len(counts), duplicate_value_count=len(values) - len(counts),
        min=min_v, max=max_v, numeric=numeric_stats, top_values=top, parse_failures=parse_failures,
        candidate_key=bool(values) and len(counts) == total,
    )


def profile_frame(df: pd.DataFrame, *, dataset: str, version_id: str, stage: str, file_bytes: int = 0,
                  encoding: str = "utf-8", delimiter: str = ",", header_detected: bool = True,
                  reject_count: int = 0) -> DatasetProfile:
    tokens = null_tokens()
    columns = [profile_column(df[c], c, tokens) for c in df.columns]
    dups = int(df.duplicated().sum()) if len(df) else 0
    stats = {"rows": len(df), "cols": [c.model_dump(exclude={"role", "role_confidence"}) for c in columns]}
    return DatasetProfile(
        dataset=dataset, dataset_version_id=version_id,
        stage=cast(Literal["raw", "clean"], stage), row_count=len(df),
        column_count=len(df.columns), file_bytes=file_bytes, encoding=encoding, delimiter=delimiter,
        header_detected=header_detected, structural_reject_count=reject_count, exact_duplicate_rows=dups,
        columns=columns, profile_hash=h(stats),
    )
