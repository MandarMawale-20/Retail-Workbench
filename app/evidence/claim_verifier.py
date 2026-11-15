"""Narrator claim verification: unknown facts, stray numbers, unit mismatches and ungrounded comparatives fail closed."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.common import looks_like_injection
from app.evidence.templates import format_fact
from app.query.schemas import Fact, NarratorOutput

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w)")
_COMPARATIVE = re.compile(r"\b(higher|lower|more|less|greater|fewer|larger|smaller|better|worse|highest|lowest|exceeds?|outperform\w*)\b", re.I)
MAX_SEGMENTS, MAX_CHARS = 14, 700


@dataclass
class Verification:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    text: str = ""


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text)]


def matches_any_fact(number: float, facts: list[Fact]) -> bool:
    for f in facts:
        if isinstance(f.value, str):
            continue
        for candidate in (f.value, abs(f.value), round(abs(f.value), 2), round(abs(f.value), 1), round(abs(f.value))):
            if abs(candidate - number) <= max(0.005, 1e-6 * abs(number)):
                return True
    return False


def ungrounded_numbers(text: str, facts: list[Fact], extra_allowed: set[float] | None = None) -> list[float]:
    allowed = extra_allowed or set()
    return [n for n in _numbers(text) if n not in allowed and not matches_any_fact(n, facts)]


def verify_narration(output: NarratorOutput, facts: list[Fact]) -> Verification:
    by_id = {f.fact_id: f for f in facts}
    has_comparison = any(f.name.startswith(("diff_top2", "pct_diff_top2", "max|", "min|")) for f in facts)
    reasons: list[str] = []
    parts: list[str] = []
    segments = output.segments
    if not segments or len(segments) > MAX_SEGMENTS:
        reasons.append("segment count out of bounds")
    for i, seg in enumerate(segments):
        if seg.type == "fact":
            fact = by_id.get(seg.fact_id or "")
            if fact is None:
                reasons.append(f"unknown fact_id {seg.fact_id!r}")
                continue
            before = segments[i - 1].text if i > 0 and segments[i - 1].type == "text" else ""
            after = segments[i + 1].text if i + 1 < len(segments) and segments[i + 1].type == "text" else ""
            if fact.unit != "percent" and re.match(r"\s*(%|percent)", after or "", re.I):
                reasons.append("unit mismatch: percent sign after non-percent fact")
            if fact.unit != "currency" and re.search(r"[₹$€£]\s*$", before or ""):
                reasons.append("unit mismatch: currency symbol before non-currency fact")
            parts.append(format_fact(fact))
        else:
            text = seg.text or ""
            if looks_like_injection(text):
                reasons.append("injection-like text in narration")
            stray = [n for n in _numbers(text) if not matches_any_fact(n, facts)]
            if stray:
                reasons.append(f"number(s) in text not backed by a fact: {stray}")
            if _COMPARATIVE.search(text) and not has_comparison:
                reasons.append("comparative wording without a comparison fact")
            parts.append(text)
    rendered = "".join(parts)
    if len(rendered) > MAX_CHARS:
        reasons.append("narration too long")
    return Verification(ok=not reasons, reasons=reasons, text=rendered if not reasons else "")
