"""Value-level parsing and column-type inference shared by profiling, issue detection and cleaning."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast

import pandas as pd

from app.config import load_yaml, thresholds

_CURRENCY = re.compile(r"[₹$€£¥]|\b(?:rs\.?|inr|usd|eur|gbp)\b", re.IGNORECASE)
_PLAIN = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_CANONICAL = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?$")
_WS = re.compile(r"\s+")
_BOOLEANS = {"true", "false", "yes", "no"}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?$")
_YMD = re.compile(r"^(\d{4})([/.])(\d{1,2})\2(\d{1,2})$")
_DM = re.compile(r"^(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})$")
_TEXT_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y")
_EPOCH = re.compile(r"^\d{10}(?:\d{3})?$")

InferredType = Literal["integer", "decimal", "string", "date", "boolean", "mixed", "empty"]


def null_tokens() -> set[str]:
    return {t.casefold() for t in load_yaml("null_tokens.yaml")}


def is_null(value: str | None, tokens: set[str] | None = None) -> bool:
    if value is None:
        return True
    return value.strip().casefold() in (tokens if tokens is not None else null_tokens())


# ---------------------------------------------------------------- numbers
def parse_number(raw: str) -> float | None:
    """Parse plain, currency-prefixed and thousands-separated numbers; anything ambiguous returns None."""
    text = raw.strip().replace("\u00a0", " ")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = _CURRENCY.sub("", text).replace(" ", "")
    if _THOUSANDS.match(text):
        text = text.replace(",", "")
    elif not _PLAIN.match(text):
        return None
    value = float(text)
    return -value if negative else value


def format_number(value: float) -> str:
    """Canonical text for a parsed number (idempotent under re-parse)."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def is_canonical_number(raw: str) -> bool:
    return bool(_CANONICAL.match(raw))


# ---------------------------------------------------------------- dates
@dataclass(frozen=True)
class DateToken:
    kind: Literal["iso", "dm"]
    year: int
    first: int          # month for iso, first component for dm
    second: int         # day for iso, second component for dm
    time: tuple[int, int, int] | None = None


def _valid(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def date_token(raw: str) -> DateToken | None:
    text = raw.strip()
    if _EPOCH.match(text):
        value = int(text)
        try:
            dt = datetime.fromtimestamp(value / (1000 if len(text) == 13 else 1), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return DateToken("iso", dt.year, dt.month, dt.day, (dt.hour, dt.minute, dt.second))
    if "T" in text or (" " in text and "-" in text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC)
        if parsed is not None and _valid(parsed.year, parsed.month, parsed.day):
            return DateToken("iso", parsed.year, parsed.month, parsed.day,
                             (parsed.hour, parsed.minute, parsed.second) if parsed.time() else None)
    if m := _ISO.match(text):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        if not _valid(y, mo, d):
            return None
        time = None
        if m[4] is not None:
            time = (int(m[4]), int(m[5]), int(m[6] or 0))
            if time[0] > 23 or time[1] > 59 or time[2] > 59:
                return None
        return DateToken("iso", y, mo, d, time)
    if m := _YMD.match(text):
        y, mo, d = int(m[1]), int(m[3]), int(m[4])
        return DateToken("iso", y, mo, d) if _valid(y, mo, d) else None
    if m := _DM.match(text):
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        if _valid(y, b, a) or _valid(y, a, b):
            return DateToken("dm", y, a, b)
        return None
    for fmt in _TEXT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return DateToken("iso", dt.year, dt.month, dt.day)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class DateAnalysis:
    kind: Literal["none", "iso", "dayfirst", "monthfirst", "ambiguous", "conflicting"]
    date_like_ratio: float
    has_time: bool
    failures: int


def _resolve_dm(tok: DateToken, kind: str) -> tuple[int, int, int] | None:
    """(year, month, day) for a dm token under a convention, or None if that reading is invalid."""
    m, d = (tok.second, tok.first) if kind == "dayfirst" else (tok.first, tok.second)
    return (tok.year, m, d) if _valid(tok.year, m, d) else None


def analyze_dates(values: list[str]) -> DateAnalysis:
    """Decide the date convention of a column; day>12 evidence is the only way to resolve DD/MM vs MM/DD."""
    if not values:
        return DateAnalysis("none", 0.0, False, 0)
    tokens = [date_token(v) for v in values]
    like = [t for t in tokens if t is not None]
    ratio = len(like) / len(values)
    if ratio < thresholds()["date_like_ratio"]:
        return DateAnalysis("none", ratio, False, len(values) - len(like))
    dm = [t for t in like if t.kind == "dm"]
    day_first = any(t.first > 12 and _resolve_dm(t, "dayfirst") for t in dm)
    month_first = any(t.second > 12 and _resolve_dm(t, "monthfirst") for t in dm)
    if not dm:
        kind = "iso"
    elif day_first and month_first:
        kind = "conflicting"
    elif day_first:
        kind = "dayfirst"
    elif month_first:
        kind = "monthfirst"
    else:
        kind = "ambiguous"
    failures = len(values) - len(like)
    if kind in ("dayfirst", "monthfirst"):
        failures += sum(1 for t in dm if _resolve_dm(t, kind) is None)
    return DateAnalysis(cast(Literal["none", "iso", "dayfirst", "monthfirst", "ambiguous", "conflicting"], kind),
                        ratio, any(t.time for t in like), failures)


def render_date(raw: str, analysis: DateAnalysis) -> str | None:
    """Canonical ISO rendering of one value under the column's resolved convention, or None on failure."""
    tok = date_token(raw)
    if tok is None:
        return None
    if tok.kind == "iso":
        y, m, d = tok.year, tok.first, tok.second
    elif analysis.kind in ("dayfirst", "monthfirst"):
        resolved = _resolve_dm(tok, analysis.kind)
        if resolved is None:
            return None
        y, m, d = resolved
    else:
        return None
    text = f"{y:04d}-{m:02d}-{d:02d}"
    if analysis.has_time:
        hh, mm, ss = tok.time or (0, 0, 0)
        text += f" {hh:02d}:{mm:02d}:{ss:02d}"
    return text


# ---------------------------------------------------------------- type inference
def collapse_ws(value: str) -> str:
    return _WS.sub(" ", value).strip()


def non_null_values(series: pd.Series, tokens: set[str] | None = None) -> list[str]:
    toks = tokens if tokens is not None else null_tokens()
    return [v for v in series.tolist() if v is not None and v.strip().casefold() not in toks]


def infer_type(values: list[str]) -> InferredType:
    """Infer the logical type of a column from its non-null string values."""
    if not values:
        return "empty"
    th = thresholds()
    n = len(values)
    numeric = [p for p in (parse_number(v) for v in values) if p is not None]
    epoch_ratio = sum(bool(_EPOCH.match(v.strip()) and 1970 <= datetime.fromtimestamp(
        int(v.strip()) / (1000 if len(v.strip()) == 13 else 1), tz=UTC).year <= 2100)
                      for v in values) / n
    if epoch_ratio >= th["date_like_ratio"]:
        return "date"
    if len(numeric) / n >= th["numeric_parse_success"]:
        return "integer" if all(p == int(p) for p in numeric) else "decimal"
    date_ratio = analyze_dates(values).date_like_ratio
    if date_ratio >= th["date_like_ratio"]:
        return "date"
    if sum(v.strip().casefold() in _BOOLEANS for v in values) / n >= th["numeric_parse_success"]:
        return "boolean"
    if len(numeric) / n >= th["mixed_type_ratio"] or date_ratio >= th["mixed_type_ratio"]:
        return "mixed"
    return "string"
