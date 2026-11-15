"""Conversation state: merge plan deltas into complete plans, track filters/metrics, bound history."""
from __future__ import annotations

from app.errors import AppError
from app.query.schemas import ConversationState, Filter, PlanDelta, QueryPlan

MAX_RECENT = 3
FILTER_RETENTION_TURNS = 6
_FILTER_REFERENCE_CUES = ("among those", "of those", "from those", "within those", "same", "only", "just",
                          "also", "what about", "how about", "and for")


def merge_delta(previous: QueryPlan | None, deltas: list[PlanDelta]) -> QueryPlan:
    """Apply deltas to the previous plan and return a complete, explicit QueryPlan."""
    plan = (previous or QueryPlan(intent="aggregate")).model_copy(deep=True)
    if previous is None and not any(d.op in ("reset", "replace_metric", "add_metric") for d in deltas):
        raise AppError("INVALID_QUERY_PLAN", "follow-up has no previous plan to modify", stage="context")
    for d in deltas:
        if d.op == "reset":
            plan = QueryPlan(intent="aggregate") if not d.carry_context else plan.model_copy(update={"filters": []})
            if not d.carry_context:
                plan.filters = []
        elif d.op in ("add_filter", "replace_filter") and d.filter:
            plan.filters = [f for f in plan.filters if f.column_id != d.filter.column_id] + [d.filter]
        elif d.op == "remove_filter":
            plan.filters = [f for f in plan.filters if f.column_id not in d.column_ids]
        elif d.op == "replace_group_by":
            plan.group_by = list(d.column_ids)
        elif d.op == "replace_metric":
            plan.metrics, plan.aggregations = list(d.metrics), []
        elif d.op == "add_metric":
            plan.metrics = [*plan.metrics, *[m for m in d.metrics if m.metric_id not in {x.metric_id for x in plan.metrics}]]
        elif d.op == "replace_sort":
            plan.sort = list(d.sort)
        elif d.op == "replace_limit" and d.limit is not None:
            plan.limit = d.limit
        plan.relationship_ids = sorted(set(plan.relationship_ids) | set(d.relationship_ids))
    outputs = set(plan.group_by) | {m.alias or m.metric_id for m in plan.metrics} | {a.alias for a in plan.aggregations}
    plan.sort = [s for s in plan.sort if s.field in outputs]
    if plan.group_by and (plan.metrics or plan.aggregations) and plan.intent in ("list", "distinct", "describe"):
        plan.intent = "top_n"
    if plan.intent in ("top_n", "aggregate", "compare") and plan.metrics and not plan.group_by and plan.intent == "top_n":
        plan.intent = "aggregate"
    return plan


def update_state(state: ConversationState, plan: QueryPlan | None, evidence_id: str | None, question: str) -> ConversationState:
    state = state.model_copy(deep=True)
    state.turn_count += 1
    state.recent_questions = [*state.recent_questions, question[:200]][-MAX_RECENT:]
    if plan is not None:
        prior_references = state.filter_last_referenced_turn
        references_existing = any(cue in question.lower() for cue in _FILTER_REFERENCE_CUES)
        references = {}
        filters = []
        for current in plan.filters:
            previous_turn = prior_references.get(current.column_id)
            last_referenced = state.turn_count if previous_turn is None or references_existing else previous_turn
            if state.turn_count - last_referenced <= FILTER_RETENTION_TURNS:
                filters.append(current)
                references[current.column_id] = last_referenced
        state.filter_last_referenced_turn = references
        state.previous_plan = plan.model_copy(update={"filters": filters})
        state.previous_evidence_id = evidence_id
        state.active_filters = filters
        state.active_metric_ids = [m.metric_id for m in plan.metrics]
    else:
        state.filter_last_referenced_turn = {
            column_id: turn
            for column_id, turn in state.filter_last_referenced_turn.items()
            if state.turn_count - turn <= FILTER_RETENTION_TURNS
        }
        state.active_filters = [
            f for f in state.active_filters
            if f.column_id in state.filter_last_referenced_turn
        ]
        if state.previous_plan is not None:
            state.previous_plan = state.previous_plan.model_copy(update={"filters": list(state.active_filters)})
    return state


def carried_filters(state: ConversationState, new_filters: list[Filter]) -> list[Filter]:
    """Previous filters that do not conflict with filters on the same column in the new question."""
    taken = {f.column_id for f in new_filters}
    return [f for f in state.active_filters if f.column_id not in taken]
