"""Relationship graph contracts. `left` is always the many side; `right` the one side."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Relationship(BaseModel):
    relationship_id: str
    graph_version: str
    left_column_id: str
    right_column_id: str
    left_dataset_version_id: str
    right_dataset_version_id: str
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
    left_null_rate: float
    right_null_rate: float
    left_duplicate_keys: int
    right_duplicate_keys: int
    matched_key_ratio: float
    unmatched_left: int
    role_compatible: bool
    status: Literal["verified", "candidate", "ambiguous", "unsafe", "rejected"]
    reasons: list[str] = Field(default_factory=list)

    @property
    def left_dataset(self) -> str:
        return self.left_column_id.split(".", 1)[0]

    @property
    def right_dataset(self) -> str:
        return self.right_column_id.split(".", 1)[0]

    def label(self) -> str:
        return f"{self.left_column_id}={self.right_column_id}"


class RelationshipGraph(BaseModel):
    graph_version: str
    relationships: list[Relationship] = Field(default_factory=list)

    def get(self, relationship_id: str) -> Relationship | None:
        return next((r for r in self.relationships if r.relationship_id == relationship_id), None)

    def between(self, a: str, b: str) -> list[Relationship]:
        return [r for r in self.relationships if {r.left_dataset, r.right_dataset} == {a, b}]
