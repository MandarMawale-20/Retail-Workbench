"""Category alias table + fuzzy matching with confidence; uncertainty is preserved, never guessed away."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.config import load_yaml, thresholds


def match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def alias_index() -> dict[str, str]:
    """normalised alias (and canonical) -> canonical label from `categories.yaml`."""
    index: dict[str, str] = {}
    for canonical, aliases in load_yaml("categories.yaml").items():
        index[match_key(canonical)] = canonical
        for alias in aliases:
            index[match_key(alias)] = canonical
    return index


@dataclass
class CategoryProposal:
    applied: dict[str, dict] = field(default_factory=dict)   # from -> {to, confidence, source}
    review: list[dict] = field(default_factory=list)         # [{values, confidence, target}]

    def affected_values(self) -> set[str]:
        return set(self.applied) | {v for r in self.review for v in r["values"]}


def _digits(value: str) -> str:
    return "".join(re.findall(r"\d", value))


def propose_mapping(counts: Counter[str]) -> CategoryProposal:
    """Propose value mappings for a categorical column from alias hits and fuzzy clusters."""
    th = thresholds()
    apply_at, review_at = th["alias_apply_confidence"], th["fuzzy_review_confidence"]
    index = alias_index()
    proposal = CategoryProposal()
    remaining: list[str] = []
    for value in sorted(counts):
        canonical = index.get(match_key(value))
        if canonical is not None and canonical != value:
            proposal.applied[value] = {"to": canonical, "confidence": 1.0, "source": "alias"}
        elif canonical is None:
            remaining.append(value)
    ordered = sorted(remaining, key=lambda v: (-counts[v], v))
    claimed: set[str] = set()
    for i, anchor in enumerate(ordered):
        if anchor in claimed:
            continue
        for other in ordered[i + 1:]:
            if other in claimed or _digits(anchor) != _digits(other):
                continue
            ratio = fuzz.ratio(match_key(anchor), match_key(other)) / 100
            if ratio >= apply_at:
                proposal.applied[other] = {"to": anchor, "confidence": round(ratio, 4), "source": "fuzzy"}
                claimed.add(other)
            elif ratio >= review_at:
                proposal.review.append({"values": [anchor, other], "confidence": round(ratio, 4), "target": anchor})
    return proposal
