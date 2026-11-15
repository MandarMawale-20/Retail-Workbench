"""Score columns against the role catalog: alias 0.5 + type 0.2 + distribution 0.2 + relationship 0.1."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal, cast

import pandas as pd
from rapidfuzz import fuzz

from app.common import tokenize_name
from app.config import thresholds
from app.roles.role_catalog import ID_ROLES, RoleSpec, load_roles
from app.schemas import ColumnProfile, DatasetProfile, RoleBinding


def _norm(name: str) -> str:
    return "_".join(tokenize_name(name))


def alias_strength(column: str, dataset: str, spec: RoleSpec) -> float:
    """1.0 exact alias, 0.8 alias as token subsequence, 0.7 fuzzy; 0 when a negative token is present."""
    norm, tokens = _norm(column), tokenize_name(column)
    if any(neg in tokens for neg in spec.negative_aliases):
        return 0.0
    best = 0.0
    candidates = list(spec.aliases)
    for alias, hints in spec.qualified_aliases.items():
        if any(hint in dataset for hint in hints):
            candidates.append(alias)
    for alias in candidates:
        a_tokens = tokenize_name(alias)
        if norm == "_".join(a_tokens):
            best = max(best, 1.0)
        elif len(a_tokens) <= len(tokens) and any(tokens[i:i + len(a_tokens)] == a_tokens
                                                 for i in range(len(tokens) - len(a_tokens) + 1)):
            best = max(best, 0.8)
        elif fuzz.ratio(norm, "_".join(a_tokens)) >= 88:
            best = max(best, 0.7)
    return best


def _distribution_ok(kind: str, col: ColumnProfile, rows: int) -> bool:
    t = col.inferred_type
    if kind == "id":
        return t in ("string", "integer") and col.distinct_count >= 2
    if kind in ("count", "money"):
        return t in ("integer", "decimal")
    if kind == "low_card":
        return t == "string" and 0 < col.distinct_count <= max(30, int(0.2 * rows))
    if kind == "status":
        return t in ("string", "boolean", "integer") and 0 < col.distinct_count <= 12
    if kind == "date":
        return t == "date"
    return t == "string"


def score_column(col: ColumnProfile, dataset: str, spec: RoleSpec, rows: int) -> tuple[float, list[str]]:
    strength = alias_strength(col.name, dataset, spec)
    if strength == 0.0 or col.inferred_type == "empty":
        return 0.0, []
    score, sources = 0.5 * strength, ["alias"]
    if col.inferred_type in spec.types:
        score += 0.2
        sources.append("type")
    if _distribution_ok(spec.distribution, col, rows):
        score += 0.2
        sources.append("distribution")
    return round(score, 4), sources


def infer_roles(profiles: dict[str, DatasetProfile], frames: dict[str, pd.DataFrame]) -> list[RoleBinding]:
    """Infer role bindings for every dataset; ambiguous ties are reported, never silently resolved."""
    th = thresholds()
    roles = load_roles()
    cands: dict[tuple[str, str], list[tuple[float, str, list[str]]]] = defaultdict(list)
    for ds, prof in profiles.items():
        for col in prof.columns:
            scored = sorted(
                ((s, r, src) for r, spec in roles.items()
                 for s, src in [score_column(col, ds, spec, prof.row_count)] if s > 0),
                key=lambda x: (-x[0], x[1]),
            )
            if scored:  # a column holds at most one role: its best-scoring one
                s, r, src = scored[0]
                cands[(ds, r)].append((s, col.name, src))
    bindings = _with_relationship_evidence(cands, profiles, frames)
    out: list[RoleBinding] = []
    for (ds, role), options in sorted(cands.items()):
        options = sorted(bindings.get((ds, role), options), key=lambda x: (-x[0], x[1]))
        ambiguous = len(options) > 1 and options[0][0] - options[1][0] < th["role_ambiguity_margin"]
        for score, name, src in (options[:2] if ambiguous else options[:1]):
            status = ("ambiguous" if ambiguous else "verified" if score >= th["role_verified"]
                      else "candidate" if score >= th["role_candidate"] else "rejected")
            out.append(RoleBinding(role=role, dataset_version_id=profiles[ds].dataset_version_id,
                                   column_id=f"{ds}.{name}", confidence=min(score, 1.0),
                                   inference_source=cast(list[Literal["alias", "type", "distribution", "relationship"]], src),
                                   status=cast(Literal["verified", "candidate", "ambiguous", "rejected"], status)))
    return out


def _with_relationship_evidence(cands, profiles, frames):
    """+0.1 when an id-role column overlaps in values with the same role in another dataset."""
    updated: dict[tuple[str, str], list[tuple[float, str, list[str]]]] = {}
    for role in ID_ROLES:
        holders = {ds: opts for (ds, r), opts in cands.items() if r == role}
        if len(holders) < 2:
            continue
        for ds, opts in holders.items():
            boosted = []
            for score, name, src in opts:
                mine = {v for v in frames[ds][name].tolist() if v is not None}
                overlap = False
                for other, other_opts in holders.items():
                    if other == ds:
                        continue
                    for _, oname, _ in other_opts:
                        theirs = {v for v in frames[other][oname].tolist() if v is not None}
                        if mine and theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= 0.3:
                            overlap = True
                boosted.append((round(score + 0.1, 4) if overlap else score, name,
                                src + ["relationship"] if overlap else src))
            updated[(ds, role)] = boosted
    return updated


def annotate_profile(profile: DatasetProfile, bindings: list[RoleBinding]) -> DatasetProfile:
    """Attach role/confidence to column profiles (verified or candidate only)."""
    by_col = {b.column_id: b for b in bindings if b.status in ("verified", "candidate")}
    for col in profile.columns:
        b = by_col.get(f"{profile.dataset}.{col.name}")
        if b:
            col.role, col.role_confidence = b.role, b.confidence
    return profile


def is_id_like(name: str, role: str | None) -> bool:
    return role in ID_ROLES or bool(re.search(r"(^|_)(id|code|sku|key|no|number)$", _norm(name)))
