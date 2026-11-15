"""Discover candidate relationships between datasets and classify cardinality and safety."""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Literal, cast

import pandas as pd

from app.common import h
from app.config import thresholds
from app.profiling.issue_detector import role_map
from app.relationships.graph import Relationship, RelationshipGraph
from app.roles.role_catalog import ID_ROLES
from app.schemas import RoleBinding


def _key_stats(frame: pd.DataFrame, column: str) -> tuple[Counter[str], float]:
    values = frame[column].tolist()
    counts = Counter(v for v in values if v is not None)
    null_rate = (len(values) - sum(counts.values())) / len(values) if values else 0.0
    return counts, round(null_rate, 6)


def _classify(a: tuple[str, Counter, float], b: tuple[str, Counter, float]) -> tuple:
    """Orient the pair so left is the 'many' side; return (left, right, cardinality, reasons)."""
    th = thresholds()
    (an, ac, _), (bn, bc, _) = a, b
    a_dup, b_dup = sum(ac.values()) - len(ac), sum(bc.values()) - len(bc)
    a_ratio = len(ac) / max(sum(ac.values()), 1)
    b_ratio = len(bc) / max(sum(bc.values()), 1)
    reasons: list[str] = []
    if a_dup == 0 and b_dup == 0:
        child, parent = (a, b) if (len(ac), an) <= (len(bc), bn) else (b, a)  # child = contained key set
        return child, parent, "one_to_one", reasons
    if a_dup == 0:
        return b, a, "many_to_one", reasons
    if b_dup == 0:
        return a, b, "many_to_one", reasons
    near_unique = max(a_ratio, b_ratio) >= th["key_candidate_ratio"]
    if near_unique:
        one, many = (a, b) if a_ratio >= b_ratio else (b, a)
        reasons.append(f"duplicate keys on the 'one' side ({one[0]}): {sum(one[1].values()) - len(one[1])}")
        return many, one, "many_to_one", reasons
    reasons.append("neither side is a unique key")
    return a, b, "many_to_many", reasons


def discover_relationships(frames: dict[str, pd.DataFrame], versions: dict[str, str],
                           bindings: list[RoleBinding]) -> RelationshipGraph:
    th = thresholds()
    roles = {ds: role_map(ds, bindings) for ds in frames}
    inputs = sorted(versions.values())
    found: list[Relationship] = []
    for a, b in combinations(sorted(frames), 2):
        for role in sorted(ID_ROLES):
            col_a = next((c for c, r in roles[a].items() if r == role), None)
            col_b = next((c for c, r in roles[b].items() if r == role), None)
            if col_a is None or col_b is None:
                continue
            ca, na = _key_stats(frames[a], col_a)
            cb, nb = _key_stats(frames[b], col_b)
            if not ca or not cb:
                continue
            left, right, cardinality, reasons = _classify((f"{a}.{col_a}", ca, na), (f"{b}.{col_b}", cb, nb))
            lid, lcounts, lnull = left
            rid, rcounts, rnull = right
            matched_keys = sum(1 for k in lcounts if k in rcounts)
            matched = round(matched_keys / len(lcounts), 6)
            unmatched_rows = sum(n for k, n in lcounts.items() if k not in rcounts)
            l_dup, r_dup = sum(lcounts.values()) - len(lcounts), sum(rcounts.values()) - len(rcounts)
            status = "verified"
            if cardinality == "many_to_many" or r_dup > 0:
                status = "unsafe"
            elif matched < th["relationship_min_matched_ratio"]:
                status, reasons = "candidate", reasons + [f"low key overlap ({matched:.2f})"]
            rel_id = "rel_" + h(lid, rid, inputs)[:12]
            found.append(Relationship(
                relationship_id=rel_id, graph_version="", left_column_id=lid, right_column_id=rid,
                left_dataset_version_id=versions[lid.split(".")[0]], right_dataset_version_id=versions[rid.split(".")[0]],
                cardinality=cardinality, left_null_rate=lnull, right_null_rate=rnull, left_duplicate_keys=l_dup,
                right_duplicate_keys=r_dup, matched_key_ratio=matched, unmatched_left=unmatched_rows,
                role_compatible=True,
                status=cast(Literal["verified", "candidate", "ambiguous", "unsafe", "rejected"], status),
                reasons=reasons))
    found = _mark_ambiguous(found)
    version = "g_" + h(sorted(r.relationship_id for r in found), inputs)[:10]
    for r in found:
        r.graph_version = version
    return RelationshipGraph(graph_version=version, relationships=sorted(found, key=lambda r: r.relationship_id))


def _mark_ambiguous(rels: list[Relationship]) -> list[Relationship]:
    """Two verified relationships between the same dataset pair are not auto-selectable."""
    groups: dict[tuple[str, str], list[Relationship]] = {}
    for r in rels:
        if r.status == "verified":
            groups.setdefault((r.left_dataset, r.right_dataset), []).append(r)
    for group in groups.values():
        if len(group) > 1:
            for r in group:
                r.status = "ambiguous"
                r.reasons.append("multiple verified key pairs between the same datasets")
    return rels
