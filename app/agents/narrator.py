"""Optional narrator: fact-referencing segments, verified by code, template fallback on any failure."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.agents.prompts import extract_first_json, narrator_messages
from app.common import sanitize_for_prompt
from app.errors import AppError
from app.evidence.claim_verifier import verify_narration
from app.query.schemas import EvidenceBundle, NarratorOutput


@dataclass
class NarratorResult:
    attempted: bool = False
    accepted: bool = False
    text: str = ""
    reasons: list[str] = field(default_factory=list)
    llm_calls: int = 0


def narrate(provider, question: str, evidence: EvidenceBundle, trace_id: str) -> NarratorResult:
    result = NarratorResult(attempted=True)
    facts = [{"id": f.fact_id, "name": sanitize_for_prompt(f.name), "value": f.value, "unit": f.unit}
             for f in evidence.facts[:40]]
    messages = narrator_messages(question, facts, evidence.metric_definitions, evidence.result_preview)
    try:
        resp = provider.complete_json(kind="narrator", messages=messages, max_tokens=500, trace_id=trace_id)
        result.llm_calls = 1
        output = NarratorOutput.model_validate(extract_first_json(resp.text))
    except AppError as exc:
        result.reasons.append(f"provider error {exc.code}")
        return result
    except (ValueError, ValidationError) as exc:
        result.reasons.append(f"invalid narrator JSON: {str(exc)[:120]}")
        return result
    verdict = verify_narration(output, evidence.facts)
    result.accepted, result.text, result.reasons = verdict.ok, verdict.text, verdict.reasons
    return result
