"""Deterministic join-path search over safe many-to-one edges (never fan-out)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.config import thresholds
from app.errors import AppError
from app.relationships.graph import Relationship, RelationshipGraph


@dataclass(frozen=True)
class JoinEdge:
    relationship_id: str
    from_dataset: str
    from_column: str
    to_dataset: str
    to_column: str


def _directed(rel: Relationship) -> list[JoinEdge]:
    forward = JoinEdge(rel.relationship_id, rel.left_dataset, rel.left_column_id, rel.right_dataset, rel.right_column_id)
    return [forward] if rel.cardinality in ("many_to_one", "one_to_one") else []


def _bfs(rels: list[Relationship], root: str, target: str, max_hops: int) -> list[JoinEdge] | None:
    edges = [e for r in rels for e in _directed(r)]
    queue: deque[tuple[str, list[JoinEdge]]] = deque([(root, [])])
    seen = {root}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        if len(path) >= max_hops:
            continue
        for e in sorted((e for e in edges if e.from_dataset == node), key=lambda e: (e.to_dataset, e.relationship_id)):
            if e.to_dataset not in seen:
                seen.add(e.to_dataset)
                queue.append((e.to_dataset, [*path, e]))
    return None


def plan_joins(graph: RelationshipGraph, root: str, targets: set[str],
               allowed_ids: set[str] | None = None) -> list[JoinEdge]:
    """Join edges (ordered, de-duplicated) letting `root` reach every target through verified many-to-one hops."""
    max_hops = thresholds()["limits"]["max_joins_per_fact"]
    verified = [r for r in graph.relationships if r.status == "verified"
                and (allowed_ids is None or r.relationship_id in allowed_ids)]
    joins: list[JoinEdge] = []
    reached = {root}
    for target in sorted(targets - {root}):
        if target in reached:
            continue
        path = _bfs(verified, root, target, max_hops)
        if path is None:
            raise _no_path(graph, root, target, allowed_ids)
        for edge in path:
            if edge.to_dataset not in reached:
                joins.append(edge)
                reached.add(edge.to_dataset)
    return joins


def _no_path(graph: RelationshipGraph, root: str, target: str, allowed_ids: set[str] | None) -> AppError:
    pool = [r for r in graph.relationships if allowed_ids is None or r.relationship_id in allowed_ids]
    unsafe = [r for r in pool if r.status != "verified" and (r.left_dataset, r.right_dataset) in ((root, target), (target, root))]
    if unsafe:
        rel = unsafe[0]
        return AppError("UNSAFE_JOIN", f"relationship {rel.label()} is {rel.status}: {'; '.join(rel.reasons) or 'not verified'}",
                        stage="validate", details={"relationship_id": rel.relationship_id, "status": rel.status,
                                                   "reasons": rel.reasons})
    if allowed_ids is not None:
        missing = _bfs([r for r in graph.relationships if r.status == "verified"], root, target,
                       thresholds()["limits"]["max_joins_per_fact"])
        if missing:
            return AppError("UNKNOWN_RELATIONSHIP", f"plan lacks relationships needed to join {root} to {target}",
                            stage="validate", details={"suggested_relationship_ids": [e.relationship_id for e in missing]})
    return AppError("UNKNOWN_RELATIONSHIP", f"no verified many-to-one path from {root} to {target}", stage="validate")


def suggest_relationship_ids(graph: RelationshipGraph, root: str, targets: set[str]) -> list[str]:
    """Relationship ids a planner should reference (empty if unreachable)."""
    try:
        return [e.relationship_id for e in plan_joins(graph, root, targets)]
    except AppError:
        return []
