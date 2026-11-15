"""Optional, safety-gated LLM suggestions for categorical cleaning."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rapidfuzz import fuzz

from app.cleaning.category import alias_index, match_key
from app.llm.base import LLMProvider


class CleaningAdvisorSuggestion(BaseModel):
    """One proposed value mapping returned by an advisor."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)
    confidence: float = Field(ge=0, le=1)


class CleaningAdvisorResponse(BaseModel):
    """The deliberately small JSON contract accepted from a cleaning advisor."""

    model_config = ConfigDict(extra="ignore")
    mappings: list[CleaningAdvisorSuggestion] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_suggestions(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"mappings": value}
        if isinstance(value, dict) and "mappings" not in value and "suggestions" in value:
            return {**value, "mappings": value["suggestions"]}
        return value


# Short aliases make the response contract convenient for callers and tests.
AdvisorSuggestion = CleaningAdvisorSuggestion
AdvisorResponse = CleaningAdvisorResponse


@dataclass
class AdvisorResult:
    applied: dict[str, dict] = field(default_factory=dict)
    review: list[dict] = field(default_factory=list)


def _canonical_target(target: str, observed: set[str], aliases: dict[str, str]) -> str | None:
    if target in observed:
        return target
    return aliases.get(match_key(target))


def _fuzzy_cooccurs(source: str, target: str, observed: set[str], aliases: dict[str, str]) -> bool:
    """Require a close observed value, while allowing a configured canonical alias."""
    if target in observed and fuzz.ratio(match_key(source), match_key(target)) / 100 >= 0.90:
        return True
    canonical = aliases.get(match_key(target))
    return canonical is not None and aliases.get(match_key(source)) == canonical


class CleaningAdvisor:
    """Calls an explicitly supplied provider and validates every suggestion locally."""

    def __init__(self, provider: LLMProvider, *, trace_id: str = "") -> None:
        self.provider = provider
        self.trace_id = trace_id

    def suggest(self, counts: dict[str, int] | Any, *, canonical: set[str] | None = None) -> AdvisorResult:
        observed = {str(value) for value in counts}
        aliases = alias_index()
        canonical = canonical or set(aliases.values())
        values = sorted(observed)
        messages = [
            {"role": "system", "content": (
                "Return JSON only with mappings, each containing from, to, confidence. "
                "Suggest only observed categorical values and canonical labels."
            )},
            {"role": "user", "content": json.dumps({"values": values, "canonical": sorted(canonical)})},
        ]
        try:
            response = self.provider.complete_json(
                kind="advisor", messages=messages, max_tokens=800, trace_id=self.trace_id
            )
            parsed = CleaningAdvisorResponse.model_validate(json.loads(response.text))
        except Exception:
            # An optional advisor must never make deterministic cleaning unavailable.
            return AdvisorResult()

        result = AdvisorResult()
        for item in parsed.mappings:
            source = item.source
            target = _canonical_target(item.target, observed, aliases)
            if source not in observed or target is None or source == target:
                continue
            safe_fuzzy = _fuzzy_cooccurs(source, target, observed, aliases)
            if item.confidence >= 0.90 and safe_fuzzy:
                result.applied[source] = {
                    "to": target, "confidence": item.confidence, "source": "llm"
                }
            else:
                result.review.append({
                    "values": [source, target], "confidence": item.confidence, "target": target
                })
        return result


def advise_category(provider: LLMProvider, counts: dict[str, int] | Any, *, trace_id: str = "") -> AdvisorResult:
    return CleaningAdvisor(provider, trace_id=trace_id).suggest(counts)
