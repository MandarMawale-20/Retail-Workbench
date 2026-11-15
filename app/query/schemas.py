"""QueryPlan and chat contracts (DATA_CONTRACTS.md §6–§8, §12). LLM-facing models forbid unknown keys."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

Scalar = str | int | float | bool
ALLOWED_TOOLS = ("get_schema", "list_distinct", "preview_join", "get_metric_definition")


class Filter(BaseModel, extra="forbid"):
    column_id: str
    op: Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "contains", "is_null", "not_null"]
    value: Scalar | list[Scalar] | None = None


class DateGrouping(BaseModel, extra="forbid"):
    """A calendar grain applied only to a resolved date/timestamp column."""
    column_id: str
    grain: Literal["month", "quarter", "year", "week", "day"]


class DateRangeFilter(BaseModel, extra="forbid"):
    """Inclusive date bounds for a resolved date/timestamp column."""
    column_id: str
    start: date
    end: date


class Aggregation(BaseModel, extra="forbid"):
    function: Literal["count", "sum", "avg", "min", "max", "distinct_count"]
    column_id: str | None = None
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")


class MetricRef(BaseModel, extra="forbid"):
    metric_id: str
    alias: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,40}$")


class SortSpec(BaseModel, extra="forbid"):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(BaseModel, extra="forbid"):
    intent: Literal["aggregate", "list", "distinct", "describe", "top_n", "compare"]
    datasets: list[str] = Field(default_factory=list)
    metrics: list[MetricRef] = Field(default_factory=list)
    aggregations: list[Aggregation] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    date_groupings: list[DateGrouping] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    date_ranges: list[DateRangeFilter] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)
    select_columns: list[str] = Field(default_factory=list)
    dataset_stage: Literal["clean", "raw"] = "clean"


class PlanDelta(BaseModel, extra="forbid"):
    op: Literal["add_filter", "remove_filter", "replace_filter", "replace_group_by", "replace_metric",
                "add_metric", "replace_sort", "replace_limit", "reset"]
    filter: Filter | None = None
    column_ids: list[str] = Field(default_factory=list)
    metrics: list[MetricRef] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1000)
    relationship_ids: list[str] = Field(default_factory=list)
    carry_context: bool = True


class ToolCall(BaseModel, extra="forbid"):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(BaseModel, extra="forbid"):
    type: Literal["tool_call", "final_plan", "final_delta", "clarify", "unanswerable"]
    calls: list[ToolCall] = Field(default_factory=list)
    plan: QueryPlan | None = None
    delta: list[PlanDelta] | None = None
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    reason: str | None = None
    missing: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    code: str
    path: str
    message: str
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bound_dataset_versions: dict[str, str] = Field(default_factory=dict)
    bound_relationships: list[str] = Field(default_factory=list)
    bound_metrics: list[dict] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    rows_considered: int
    result_row_count: int
    duration_ms: int
    sql_fingerprint: str
    truncated: bool = False


class Fact(BaseModel):
    fact_id: str
    name: str
    value: float | int | str
    unit: Literal["count", "currency", "percent", "ratio", "text"]
    formula_id: str
    source_ref: str


class EvidenceBundle(BaseModel):
    evidence_id: str
    trace_id: str
    dataset_versions: dict[str, str]
    dataset_stage: str
    graph_version: str | None
    relationships: list[dict]
    metric_definitions: list[dict]
    role_bindings: list[dict]
    selected_columns: list[str]
    filters: list[dict]
    operations: list[dict]
    aggregation_grain: list[str]
    null_policy: str | None
    rows_considered: int
    result_row_count: int
    result_preview: list[dict]
    facts: list[Fact]
    assumptions: list[str]
    sql_fingerprint: str


class ChatResult(BaseModel):
    status: Literal["ok", "clarification_required", "unsupported", "refused", "error"]
    answer_text: str
    answer_source: Literal["template", "narrator", "none"] = "none"
    plan: QueryPlan | None = None
    validation: ValidationResult | None = None
    evidence: EvidenceBundle | None = None
    clarification: dict | None = None
    error: dict | None = None
    trace_id: str = ""
    turn_id: str = ""


class ConversationState(BaseModel):
    session_id: str
    owner_id: str
    active_dataset_version_ids: list[str] = Field(default_factory=list)
    graph_version: str | None = None
    previous_plan: QueryPlan | None = None
    previous_evidence_id: str | None = None
    active_filters: list[Filter] = Field(default_factory=list)
    filter_last_referenced_turn: dict[str, int] = Field(default_factory=dict)
    active_metric_ids: list[str] = Field(default_factory=list)
    pending_clarification: dict | None = None
    recent_questions: list[str] = Field(default_factory=list)
    turn_count: int = 0


class NarratorSegment(BaseModel, extra="forbid"):
    type: Literal["text", "fact"]
    text: str | None = None
    fact_id: str | None = None


class NarratorOutput(BaseModel, extra="forbid"):
    segments: list[NarratorSegment]


class ToolCallRecord(BaseModel):
    seq: int
    tool: Literal["get_schema", "list_distinct", "preview_join", "get_metric_definition"]
    args: dict
    result_hash: str
    duration_ms: int
    source: Literal["llm_requested", "code_injected", "fallback"]


class ToolLog(BaseModel):
    calls: list[ToolCallRecord] = Field(default_factory=list)

    def has(self, tool: str, **args: Any) -> bool:
        return any(c.tool == tool and all(c.args.get(k) == v for k, v in args.items()) for c in self.calls)


class RequiredEvidence(BaseModel):
    list_distinct: list[str] = Field(default_factory=list)
    preview_join: list[str] = Field(default_factory=list)
    get_metric_definition: list[str] = Field(default_factory=list)
