"""Clarification gate: ask only on real ambiguity (no documented default). Otherwise proceed and state the assumption."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from app.config import load_yaml
from app.metrics.binder import try_bind
from app.query.catalog import Catalog
from app.query.schemas import ConversationState

_AGG_WORDS = re.compile(r"\b(total|sum|average|avg|mean|count|how many|number of|distinct|unique|list|describe)\b", re.I)
_DATE_WORDS = re.compile(r"\b(january|february|march|april|june|july|august|september|october|november|december|"
                         r"(?:in|during|of) may|last month|this month|last year|this year|per month|monthly|by month|"
                         r"quarter|weekly|by week|daily|by day|yesterday|today|between \d)\b", re.I)
_RANK = re.compile(r"\b(best|top|performed|performing|performance|leading|worst|bottom)\b", re.I)
_DIM = re.compile(r"\b(products?|items?|stores?|categories|category|customers?|regions?)\b", re.I)

METRIC_OPTIONS = {"units_sold": "Units sold", "gross_sales_revenue": "Gross sales revenue"}


def metric_phrase_present(question: str) -> bool:
    q = question.lower()
    return any(p in q for phrases in load_yaml("metrics.yaml")["metric_synonyms"].values() for p in phrases)


def check_clarification(question: str, catalog: Catalog, state: ConversationState) -> dict | None:
    """Return a clarification payload or None."""
    if _DATE_WORDS.search(question):
        amb = [c for c in catalog.all_columns() if c.date_ambiguous]
        if amb:
            return {"kind": "date_format", "question": f"'{amb[0].column_id}' mixes day and month ambiguously (DD/MM/YYYY vs MM/DD/YYYY), "
                    "so it was left unparsed and I will not guess. Which format does it use?",
                    "options": ["DD/MM/YYYY", "MM/DD/YYYY"],
                    "note": "Approve the skipped parse_date_iso step (interactive_review) to enable date questions.",
                    "column_id": amb[0].column_id}
    if _RANK.search(question) and _DIM.search(question) and not metric_phrase_present(question) \
            and not _AGG_WORDS.search(question) and not state.active_metric_ids:
        supported = [m for m in METRIC_OPTIONS if try_bind(m, catalog.role_index)]
        if len(supported) >= 2:
            return {"kind": "metric", "question": "Rank by units sold or by gross sales revenue?",
                    "options": [METRIC_OPTIONS[m] for m in supported]}
    return None


def resolve_pending(question: str, pending: dict | None) -> str | None:
    """If `question` answers a pending clarification, return the completed original question."""
    if not pending or pending.get("kind") != "metric":
        return None
    original, options = pending.get("original", ""), pending.get("options", [])
    q = question.strip().lower()
    best = max(options, key=lambda o: fuzz.partial_ratio(q, o.lower()), default=None)
    if best and fuzz.partial_ratio(q, best.lower()) >= 75:
        return f"{original} (by {best.lower()})"
    if q in ("1", "first") and options:
        return f"{original} (by {options[0].lower()})"
    if q in ("2", "second") and len(options) > 1:
        return f"{original} (by {options[1].lower()})"
    return None
