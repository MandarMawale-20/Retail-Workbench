"""Schema linker: prune the catalog to a bounded, scored context for the planner and enforce the token budget."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.planner_tools import is_pii_column
from app.agents.prompts import PLANNER_SYSTEM, estimate_tokens
from app.common import sanitize_for_prompt, tokenize_name
from app.config import load_yaml, thresholds
from app.errors import AppError
from app.metrics.binder import metric_definition_summary, try_bind
from app.metrics.definitions import all_metrics
from app.query.catalog import Catalog, ColumnInfo
from app.query.schemas import ConversationState

_MIN_COLUMNS = 0


@dataclass
class SchemaContext:
    payload: dict
    columns: list[ColumnInfo]
    trim_log: list[str] = field(default_factory=list)
    list_distinct_limit: int = 50
    flagged: list[str] = field(default_factory=list)


def _question_tokens(question: str) -> set[str]:
    words = set(tokenize_name(question))
    dims = load_yaml("metrics.yaml")["dimension_words"]
    for role, aliases in dims.items():
        if any(a in question.lower() for a in aliases):
            words.add(role)
            words.update(tokenize_name(role))
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}


def score_column(col: ColumnInfo, tokens: set[str]) -> float:
    name_tokens = {t[:-1] if t.endswith("s") and len(t) > 3 else t for t in tokenize_name(col.name)}
    score = 2.0 * len(tokens & name_tokens)
    if col.role:
        score += 1.0 + (2.0 if col.role in tokens else 0.0)
    if col.is_id:
        score += 0.5
    return score


def build_schema_context(question: str, catalog: Catalog, state: ConversationState, *, max_tokens: int) -> SchemaContext:
    limits = thresholds()["limits"]
    tokens = _question_tokens(question)
    scored = sorted(((score_column(c, tokens), c) for c in catalog.all_columns()
                     if not is_pii_column(c.name) and sanitize_for_prompt(c.column_id) == c.column_id),
                    key=lambda sc: (-sc[0], sc[1].column_id))
    keep = [c for _, c in scored[: limits["max_columns_in_context"]]]
    ds_scores: dict[str, float] = {}
    for s, c in scored:
        ds_scores[c.dataset] = ds_scores.get(c.dataset, 0) + s
    datasets = sorted(sorted(ds_scores, key=lambda d: (-ds_scores[d], d))[: limits["max_datasets"]])
    ctx = SchemaContext(payload={}, columns=keep)
    describe_metrics = True

    def render() -> dict:
        cols = [c for c in ctx.columns if c.dataset in datasets]
        rels = [r for r in catalog.graph.relationships if r.left_dataset in datasets and r.right_dataset in datasets]
        metrics = []
        for m in all_metrics().values():
            summary = metric_definition_summary(m, try_bind(m.metric_id, catalog.role_index))
            if not describe_metrics:
                summary.pop("description", None)
                summary.pop("formula", None)
            metrics.append(summary)
        return {
            "datasets": [{"name": d, "rows": catalog.datasets[d].rows_clean,
                          "columns": [{"id": c.column_id, "type": c.sql_type, "role": c.role, "null_pct": round(c.null_pct, 2),
                                       "distinct": c.distinct_count} for c in cols if c.dataset == d]} for d in datasets],
            "relationships": [{"id": r.relationship_id, "from": r.left_column_id, "to": r.right_column_id,
                               "cardinality": r.cardinality, "status": r.status} for r in rels],
            "metrics": metrics,
            "state": {"active_filters": [f.model_dump() for f in state.active_filters],
                      "active_metrics": state.active_metric_ids,
                      "last_plan": state.previous_plan.model_dump(exclude_defaults=True) if state.previous_plan else None},
            "date_columns_ambiguous": [c.column_id for c in catalog.all_columns() if c.date_ambiguous],
            "list_distinct_limit": ctx.list_distinct_limit,
        }

    ctx.payload = render()
    base = estimate_tokens(PLANNER_SYSTEM, state.recent_questions[-3:], question)
    while base + estimate_tokens(ctx.payload) > max_tokens:
        if len(ctx.columns) > _MIN_COLUMNS:
            ctx.columns = ctx.columns[:-1]
            ctx.trim_log.append("dropped lowest-scoring column")
        elif ctx.list_distinct_limit > 20:
            ctx.list_distinct_limit = 20
            ctx.trim_log.append("list_distinct limit 50->20")
        elif describe_metrics:
            describe_metrics = False
            ctx.trim_log.append("dropped metric descriptions")
        else:
            raise AppError("UNSUPPORTED_QUERY", "question needs more context than the budget allows", stage="link",
                           details={"budget_tokens": max_tokens})
        ctx.payload = render()
    ctx.trim_log = list(dict.fromkeys(ctx.trim_log))
    return ctx
