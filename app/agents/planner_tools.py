"""Read-only planner tools executed by code. Results are untrusted data and are sanitised before fencing."""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from app.common import h, looks_like_injection, sanitize_for_prompt
from app.errors import AppError
from app.metrics.binder import metric_definition_summary, try_bind
from app.metrics.definitions import get_metric
from app.query.catalog import Catalog
from app.query.schemas import ALLOWED_TOOLS, ToolCallRecord, ToolLog

_PII_NAME = re.compile(r"(e[-_ ]?mail|phone|mobile|address|ssn|dob|birth|first_?name|last_?name|customer_?name|full_?name|contact|passport)", re.I)
_PII_VALUE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d")
MAX_DISTINCT = 50


def is_pii_column(name: str) -> bool:
    return bool(_PII_NAME.search(name))


class PlannerTools:
    """Executes the four allow-listed tools and records each call in the per-question ToolLog."""

    def __init__(self, catalog: Catalog, values: Callable[[str, str], list[tuple[str, int]]], tool_log: ToolLog,
                 default_limit: int = MAX_DISTINCT) -> None:
        self.catalog, self.values, self.log = catalog, values, tool_log
        self.default_limit = default_limit
        self.flagged: list[str] = []

    def run(self, tool: str, args: dict[str, Any], source: str) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise AppError("INVALID_QUERY_PLAN", f"tool '{tool}' is not allowed", stage="planner", details={"blocked_at": "tool_allowlist"})
        start = time.perf_counter()
        handler = getattr(self, f"_{tool}")
        try:
            result = handler(**args)
        except TypeError:
            result = {"error": "invalid arguments"}
        record = ToolCallRecord(seq=len(self.log.calls), tool=tool, args=args, result_hash=h(result)[:16],  # type: ignore[arg-type]
                                duration_ms=int((time.perf_counter() - start) * 1000), source=source)  # type: ignore[arg-type]
        self.log.calls.append(record)
        return {"tool": tool, "args": args, "result": result}

    def _get_schema(self, dataset: str) -> dict:
        info = self.catalog.datasets.get(dataset)
        if info is None:
            return {"error": f"unknown dataset '{dataset}'"}
        return {"dataset": dataset, "rows": info.rows_clean, "columns": [
            {"id": c.column_id, "type": c.sql_type, "role": c.role, "null_pct": round(c.null_pct, 3), "distinct": c.distinct_count}
            for c in info.columns.values() if not is_pii_column(c.name)][:40]}

    def _list_distinct(self, column_id: str, limit: int = MAX_DISTINCT) -> dict:
        info = self.catalog.column(column_id)
        if info is None:
            return {"error": f"unknown column '{column_id}'"}
        if is_pii_column(info.name):
            return {"error": "column is not available (personal data)"}
        limit = max(1, min(int(limit), self.default_limit))
        out, flagged = [], False
        for value, count in self.values(column_id, "clean")[:limit]:
            if _PII_VALUE.search(value):
                continue
            flagged |= looks_like_injection(value)
            out.append({"v": sanitize_for_prompt(value), "n": count})
        if flagged:
            self.flagged.append(column_id)
        return {"column_id": column_id, "values": out, "truncated": len(out) >= limit}

    def _preview_join(self, relationship_id: str) -> dict:
        rel = self.catalog.graph.get(relationship_id)
        if rel is None:
            return {"error": f"unknown relationship '{relationship_id}'"}
        return {"relationship_id": rel.relationship_id, "from": rel.left_column_id, "to": rel.right_column_id,
                "cardinality": rel.cardinality, "left_duplicate_keys": rel.left_duplicate_keys,
                "right_duplicate_keys": rel.right_duplicate_keys, "matched_key_ratio": rel.matched_key_ratio,
                "status": rel.status, "reasons": [sanitize_for_prompt(r, 120) for r in rel.reasons]}

    def _get_metric_definition(self, metric_id: str) -> dict:
        metric = get_metric(metric_id)
        if metric is None:
            return {"error": f"unknown metric '{metric_id}'"}
        return metric_definition_summary(metric, try_bind(metric_id, self.catalog.role_index))
