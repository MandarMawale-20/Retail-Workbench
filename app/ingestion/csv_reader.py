"""All-VARCHAR CSV import through DuckDB with explicit header handling and captured structural rejects."""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.common import normalize_header
from app.errors import AppError

_DELIMITERS = [",", ";", "\t", "|"]


@dataclass
class CsvReadResult:
    frame: pd.DataFrame
    rejects: list[dict[str, Any]] = field(default_factory=list)
    delimiter: str = ","
    header_detected: bool = True


def detect_delimiter(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        lines = [ln for _, ln in zip(range(30), fh, strict=False) if ln.strip()]
    best, best_score = ",", 0
    for delim in _DELIMITERS:
        counts = Counter(len(r) for r in csv.reader(lines, delimiter=delim))
        if not counts:
            continue
        width, freq = counts.most_common(1)[0]
        score = (width - 1) * freq if width > 1 else 0
        if score > best_score:
            best, best_score = delim, score
    return best


def _detect_header(con: duckdb.DuckDBPyConnection, path: Path, delim: str) -> bool:
    try:
        row = con.execute("SELECT HasHeader FROM sniff_csv(?, delim=?)", [str(path), delim]).fetchone()
        return True if row is None else bool(row[0])
    except duckdb.Error:
        return True


def _unique_headers(names: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for i, raw in enumerate(names):
        name = normalize_header(raw) or f"column_{i + 1}"
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name] - 1}")
    return out


def to_object_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise to object dtype holding `str | None` (missing is always None)."""
    out = df.astype(object)
    return out.where(out.notna(), None)


def _first_record(path: Path, delim: str) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh, delimiter=delim):
            if any(cell.strip() for cell in row):
                return row
    return []


def read_csv_as_strings(path: Path, *, max_rows: int) -> CsvReadResult:
    """Read `path` (UTF-8) with every column as VARCHAR.

    The dialect sniffer is bypassed: delimiter and column count come from the first record, so malformed lines are
    rejected and recorded (copied out before the connection closes) instead of aborting the whole import.
    """
    con = duckdb.connect(":memory:")
    try:
        delim = detect_delimiter(path)
        first = _first_record(path, delim)
        if not first:
            raise AppError("INVALID_FILE", "file has no readable records", stage="ingest")
        header = _detect_header(con, path, delim)
        cols = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(len(first)))
        query = (f"SELECT * FROM read_csv(?, columns={{{cols}}}, auto_detect=false, header=?, delim=?, quote='\"', "
                 "store_rejects=true, parallel=false)")
        df = con.execute(query, [str(path), header, delim]).df()
        rejects: list[dict[str, Any]] = []
        try:
            for line, error_type, csv_line, message in con.execute(
                "SELECT line, error_type, csv_line, error_message FROM reject_errors ORDER BY line"
            ).fetchall():
                if any(r["line"] == line for r in rejects):
                    continue
                rejects.append({"line": line, "error_type": error_type,
                                "csv_line": (csv_line or "")[:200], "message": message})
        except duckdb.Error:
            rejects = []
    except duckdb.Error as exc:
        raise AppError("INVALID_FILE", f"CSV could not be parsed: {exc}", stage="ingest") from exc
    finally:
        con.close()
    if len(df) > max_rows:
        raise AppError("INVALID_FILE", f"file exceeds {max_rows} rows", stage="ingest")
    names = first if header else [f"col_{i + 1}" for i in range(len(first))]
    df.columns = _unique_headers(names)
    return CsvReadResult(frame=to_object_frame(df), rejects=rejects, delimiter=delim, header_detected=header)
