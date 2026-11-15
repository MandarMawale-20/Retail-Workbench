"""Pipeline contracts (see DATA_CONTRACTS.md §3). Every contract is a Pydantic model."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

InferredTypeName = Literal["integer", "decimal", "string", "date", "boolean", "mixed", "empty"]
Risk = Literal["low", "medium", "high"]


class ExecutionContext(BaseModel):
    """Server-owned scope; never produced by an LLM."""
    owner_id: str
    session_id: str | None = None
    active_dataset_version_ids: list[str] = Field(default_factory=list)
    dataset_stage: Literal["clean", "raw"] = "clean"
    graph_version: str | None = None
    policy_version: str = ""
    trace_id: str = ""


class PipelineOptions(BaseModel):
    policy: Literal["unattended", "interactive_review"] = "unattended"
    force: bool = False
    use_advisor: bool = True
    max_upload_mb: int = 100
    approved_step_ids: list[str] = Field(default_factory=list)


class ColumnProfile(BaseModel):
    name: str
    inferred_type: InferredTypeName
    null_count: int
    null_pct: float
    distinct_count: int
    duplicate_value_count: int
    min: str | None = None
    max: str | None = None
    numeric: dict | None = None
    top_values: list[tuple[str, int]] = Field(default_factory=list)
    parse_failures: dict[str, int] = Field(default_factory=dict)
    candidate_key: bool = False
    role: str | None = None
    role_confidence: float | None = None


class DatasetProfile(BaseModel):
    dataset: str
    dataset_version_id: str
    stage: Literal["raw", "clean"]
    row_count: int
    column_count: int
    file_bytes: int
    encoding: str
    delimiter: str
    header_detected: bool
    structural_reject_count: int
    exact_duplicate_rows: int
    columns: list[ColumnProfile]
    profile_hash: str


class RoleBinding(BaseModel):
    role: str
    dataset_version_id: str
    column_id: str
    confidence: float
    inference_source: list[Literal["alias", "type", "distribution", "relationship"]]
    status: Literal["verified", "candidate", "ambiguous", "rejected"]


IssueType = Literal[
    "whitespace", "case_variants", "null_tokens", "mixed_numeric", "numeric_parse_failures",
    "date_format_ambiguous", "date_parse_failures", "non_iso_dates", "exact_duplicate_rows", "duplicate_ids",
    "category_variants", "all_null_column", "negative_values", "structural_rejects", "header_only",
    "unresolved_role",
]


class Issue(BaseModel):
    issue_id: str
    dataset: str
    column: str | None = None
    issue_type: IssueType
    severity: Risk
    count: int
    examples: list[str] = Field(default_factory=list)
    status: Literal["detected", "planned", "resolved", "needs_review"] = "detected"


class CleaningStep(BaseModel):
    step_id: str
    issue_id: str
    dataset_version_id: str
    dataset: str
    columns: list[str]
    transformation: str
    parameters: dict = Field(default_factory=dict)
    reason: str
    risk: Risk
    source: Literal["rule", "llm", "user"] = "rule"
    confidence: float | None = None
    status: Literal["proposed", "approved", "applied", "skipped_needs_review", "rejected", "failed"] = "proposed"
    rows_affected: int | None = None


class ValidationReport(BaseModel):
    passed: bool
    checks: list[dict]
    rows_accepted: int
    rows_clean: int
    exact_dups_removed: int
    unresolved_issues: list[str] = Field(default_factory=list)
