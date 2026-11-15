"""Read-only DuckDB execution with a hard timeout and row cap."""
from __future__ import annotations

import math
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from app.config import Settings
from app.errors import AppError
from app.query.catalog import Catalog, quote_ident
from app.query.compiler import CompiledQuery
from app.query.schemas import ExecutionResult


def open_readonly(catalog: Catalog, settings: Settings) -> duckdb.DuckDBPyConnection:
    """Analytics connection: read-only, no external access, locked configuration."""
    return duckdb.connect(str(catalog.db_path), read_only=True, config={
        "enable_external_access": "false", "memory_limit": settings.duckdb_memory_limit,
        "threads": str(settings.duckdb_threads), "lock_configuration": "true"})


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, 6)
    if isinstance(v, datetime | date):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    return v


def execute_query(compiled: CompiledQuery, catalog: Catalog, settings: Settings) -> ExecutionResult:
    started = time.perf_counter()
    con = open_readonly(catalog, settings)
    timer = threading.Timer(settings.query_timeout_s, con.interrupt)
    timer.start()
    try:
        rows = con.execute(compiled.sql, compiled.params).fetchall()
        count_row = con.execute(compiled.count_sql, compiled.count_params).fetchone()
        considered = count_row[0] if count_row is not None else 0
    except duckdb.InterruptException as exc:
        raise AppError("QUERY_TIMEOUT", f"query exceeded {settings.query_timeout_s}s", retryable=True, stage="execute") from exc
    except duckdb.Error as exc:
        raise AppError("INTERNAL_ERROR", f"query failed: {exc}", stage="execute") from exc
    finally:
        timer.cancel()
        con.close()
    truncated = len(rows) > compiled.limit
    rows = rows[: compiled.limit]
    return ExecutionResult(columns=compiled.columns, rows=[[_json_value(v) for v in r] for r in rows],
                           rows_considered=int(considered), result_row_count=len(rows),
                           duration_ms=int((time.perf_counter() - started) * 1000),
                           sql_fingerprint=compiled.fingerprint, truncated=truncated)


def distinct_values(catalog: Catalog, settings: Settings, column_id: str, stage: str = "clean",
                    limit: int = 5000) -> list[tuple[str, int]]:
    """Distinct non-null values with counts (case/space variants kept as stored)."""
    ds, _, col = column_id.partition(".")
    con = open_readonly(catalog, settings)
    try:
        rows = con.execute(
            f"SELECT CAST({quote_ident(col)} AS VARCHAR) AS v, COUNT(*) AS n FROM {catalog.table(ds, stage)} "
            f"WHERE {quote_ident(col)} IS NOT NULL GROUP BY 1 ORDER BY n DESC, v LIMIT ?", [limit]).fetchall()
    finally:
        con.close()
    return [(r[0], int(r[1])) for r in rows]
