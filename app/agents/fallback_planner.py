"""Deterministic rule-based planner (LLM-free). It never guesses a column: ties clarify, unknowns are unsupported."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal, cast

from rapidfuzz import fuzz

from app.agents.planner import PlannerOutcome
from app.agents.planner_tools import PlannerTools, is_pii_column
from app.common import normalize_value, tokenize_name
from app.config import load_yaml
from app.conversation.context import carried_filters
from app.errors import AppError
from app.query.catalog import Catalog, ColumnInfo
from app.query.schemas import (
    Aggregation,
    ConversationState,
    DateGrouping,
    DateRangeFilter,
    Filter,
    MetricRef,
    PlanDelta,
    QueryPlan,
    Scalar,
    SortSpec,
)
from app.query.validator import missing_evidence, required_evidence
from app.relationships.join_policy import suggest_relationship_ids

_STOP = {"the", "a", "an", "of", "in", "for", "each", "every", "all", "our", "my", "by", "per", "top", "bottom", "which", "what",
         "only", "just", "highest", "lowest", "best", "worst", "most", "least", "is", "are", "and", "with", "from", "among", "those",
         "has", "have", "had", "performed", "sold", "generated", "does", "did", "was", "were", "or", "that", "this", "these", "then"}
_CUE = re.compile(r"\b(only|just|among those|of those|from those|within those|same but|what about|how about|and for|instead|also)\b")
_CARRY = re.compile(r"\b(among those|of those|from those|within those|same|only|just|also|what about|how about|and for)\b")
_BOTTOM = re.compile(r"\b(bottom|lowest|least|worst|fewest|smallest)\b")
_LOW_STOCK = re.compile(r"\b(low|lowest|running low|short) (on |in )?(stock|inventory)\b")
_OUT_OF_STOCK = re.compile(r"\b(out of stock|stock[- ]?out|zero stock|no stock)\b")
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "five": 5, "ten": 10}
_ID_TOKEN = re.compile(r"\b[A-Za-z]{1,3}\d{2,}\b")
ID_ROLES_FOR_VALUES = ("product_id", "store_id", "customer_id", "order_id")
SINGULAR_DIMS = {"category", "region", "store", "product", "customer", "item", "outlet"}


class FallbackPlanner:
    """Maps a question to a QueryPlan/PlanDelta using the catalog, role bindings and metric registry."""

    def __init__(self, catalog: Catalog, tools: PlannerTools, state: ConversationState) -> None:
        self.catalog, self.tools, self.state = catalog, tools, state
        cfg = load_yaml("metrics.yaml")
        self.cfg = cfg
        self.phrases = sorted(((p, m) for m, ps in cfg["metric_synonyms"].items() for p in ps), key=lambda x: -len(x[0]))

    # ------------------------------------------------------------------ entry
    def plan(self, question: str) -> PlannerOutcome:
        q = " ".join(question.split())
        ql = q.lower()
        try:
            return self._plan(q, ql)
        except AppError as exc:
            return PlannerOutcome(kind="unanswerable", reason=exc.message, missing=(exc.details or {}).get("missing_roles", []),
                                  source="fallback")

    # ------------------------------------------------------------------ main logic
    def _plan(self, q: str, ql: str) -> PlannerOutcome:
        unsupported = self._unsupported(ql)
        if unsupported is not None:
            return unsupported
        stage = "raw" if re.search(r"\b(raw|uncleaned|un-cleaned|original)\b", ql) else "clean"
        rows = self._row_count(ql, stage)
        if rows is not None:
            return rows
        low_stock = bool(_LOW_STOCK.search(ql))
        out_of_stock = bool(_OUT_OF_STOCK.search(ql))
        scan = _LOW_STOCK.sub(" ", ql) if low_stock else ql
        scan = _OUT_OF_STOCK.sub(" ", scan) if out_of_stock else scan
        metrics = self._metrics(scan)
        if re.search(r"\b(gross)\b.*\b(net)\b|\b(net)\b.*\b(gross)\b", ql) and re.search(r"\b(vs\.?|versus|and|compare|or)\b", ql):
            metrics = [m for m in metrics if m not in ("gross_sales_revenue", "net_sales_revenue")] + ["gross_sales_revenue", "net_sales_revenue"]
        if low_stock and "inventory_on_hand" not in metrics:
            metrics.append("inventory_on_hand")
        filters = self._filters(q, ql)
        group_cols = self._group_by(ql, filters)
        date_groupings, date_ranges, date_question = self._time_dimensions(ql)
        if date_question:
            return date_question
        direction: Literal["asc", "desc"] = "asc" if _BOTTOM.search(ql) else "desc"
        limit = self._limit(ql, group_cols)
        aggs: list[Aggregation] = []
        intent_kind = None

        if out_of_stock:
            stock_col = next((c for c in self.catalog.columns_with_role("available_stock") if c.is_numeric), None)
            product_col = next((c for c in self.catalog.columns_with_role("product_id")
                                if stock_col and c.dataset == stock_col.dataset), None)
            if stock_col is None or product_col is None:
                return PlannerOutcome(kind="unanswerable", reason="out-of-stock queries require product and stock columns",
                                      missing=["product_id", "available_stock"], source="fallback")
            filters.append(Filter(column_id=stock_col.column_id, op="lte", value=0))
            plan = QueryPlan(intent="list", select_columns=[product_col.column_id], group_by=[],
                             filters=filters, limit=limit,
                             dataset_stage=cast(Literal["clean", "raw"], stage))
            return self._finish(plan, ql)

        if not metrics:
            special = self._special(q, ql, filters, group_cols)
            if isinstance(special, PlannerOutcome):
                return special
            if special:
                intent_kind, aggs, select_cols, group_cols = special
                plan = QueryPlan(intent=intent_kind, datasets=sorted({c.split('.')[0] for c in (select_cols or group_cols or [])}),
                                 aggregations=aggs, select_columns=select_cols, group_by=group_cols, filters=filters,
                                 date_groupings=date_groupings, date_ranges=date_ranges,
                                 limit=limit if intent_kind in ("distinct", "list") else 100,
                                 dataset_stage=cast(Literal["clean", "raw"], stage))
                return self._finish(plan, ql)

        follow_up = self.state.previous_plan is not None and bool(_CUE.search(ql))
        previous_plan = self.state.previous_plan
        if follow_up and metrics and not group_cols and previous_plan is not None and previous_plan.group_by:
            group_cols = list(previous_plan.group_by)
        if follow_up and not metrics and not group_cols and filters:
            deltas = [PlanDelta(op="replace_filter" if any(f.column_id == x.column_id for x in self.state.active_filters)
                                else "add_filter", filter=f) for f in filters]
            if re.search(r"\btop \d+|\bbottom \d+", ql):
                deltas.append(PlanDelta(op="replace_limit", limit=limit))
            return PlannerOutcome(kind="delta", delta=deltas, source="fallback")
        if not metrics:
            return self._no_metric(q, ql, filters, group_cols)

        intent = "compare" if re.search(r"\b(compare|vs\.?|versus)\b", ql) else \
            "top_n" if group_cols and (re.search(r"\b(top|bottom|best|worst|highest|lowest|most|least|which)\b", ql)) else "aggregate"
        if intent == "compare" and not group_cols:
            col = next((f.column_id for f in filters if f.op == "in"), None)
            if col is None and len({f.column_id for f in filters}) == 1 and len(filters) > 1:
                col = filters[0].column_id
            if col:
                values = [v for f in filters if f.column_id == col
                          for v in (f.value if isinstance(f.value, list) else [f.value]) if v is not None]
                filters = [f for f in filters if f.column_id != col] + [Filter(column_id=col, op="in", value=values)]
                group_cols = [col]
        sort = [SortSpec(field=metrics[0], direction=direction)] if group_cols else []
        if low_stock and "inventory_on_hand" in metrics and len(metrics) > 1 and group_cols:
            sort = [SortSpec(field=m, direction="asc" if m == "inventory_on_hand" else "desc") for m in metrics if m != "inventory_on_hand"][:1] \
                + [SortSpec(field="inventory_on_hand", direction="asc")]
        if self.state.previous_plan and _CARRY.search(ql):
            filters = filters + carried_filters(self.state, filters)
        plan = QueryPlan(intent=cast(Literal["aggregate", "list", "distinct", "describe", "top_n", "compare"], intent),
                         metrics=[MetricRef(metric_id=m) for m in metrics], group_by=group_cols,
                         date_groupings=date_groupings, date_ranges=date_ranges, filters=filters,
                         sort=sort, limit=limit, dataset_stage=cast(Literal["clean", "raw"], stage))
        return self._finish(plan, ql)

    # ------------------------------------------------------------------ pieces
    def _row_count(self, ql: str, stage: str) -> PlannerOutcome | None:
        if not re.search(r"\b(how many rows|row count|number of rows|rows in)\b", ql):
            return None
        ds = next((d for d in sorted(self.catalog.datasets, key=len, reverse=True) if re.search(rf"\b{re.escape(d)}\b", ql)), None)
        if ds is None:
            return PlannerOutcome(kind="clarify", question="Which dataset should I count rows in?",
                                  options=sorted(self.catalog.datasets), source="fallback")
        plan = QueryPlan(intent="aggregate", datasets=[ds], aggregations=[Aggregation(function="count", alias="row_count")],
                         dataset_stage=cast(Literal["clean", "raw"], stage))
        return PlannerOutcome(kind="plan", plan=plan, source="fallback")

    def _unsupported(self, ql: str) -> PlannerOutcome | None:
        for term, roles in self.cfg["unsupported_terms"].items():
            if re.search(rf"\b{re.escape(term)}\b", ql):
                missing = [r for r in roles if not self.catalog.columns_with_role(r)]
                reason = (f"'{term}' needs data that is not available: missing role(s) {', '.join(missing)}" if missing
                          else f"'{term}' is not a registered metric")
                return PlannerOutcome(kind="unanswerable", reason=reason, missing=missing, source="fallback")
        return None

    def _metrics(self, ql: str) -> list[str]:
        work, found = ql, []
        for phrase, metric in self.phrases:
            m = re.search(rf"\b{re.escape(phrase)}\b", work)
            if m:
                found.append((m.start(), metric))
                work = work[:m.start()] + " " * (m.end() - m.start()) + work[m.end():]
        ordered: list[str] = []
        for _, metric in sorted(found):
            if metric not in ordered:
                ordered.append(metric)
        return ordered

    def _limit(self, ql: str, group_cols: list[str]) -> int:
        m = re.search(r"\b(?:top|bottom|first|lowest|highest)\s+(\d+)\b", ql)
        if m:
            return max(1, min(int(m.group(1)), 1000))
        m = re.search(r"\b(?:top|bottom)\s+(one|two|three|five|ten)\b", ql)
        if m:
            return _NUM_WORDS[m.group(1)]
        if re.search(r"\bwhich (\w+) (?:has|had|is|was|performed|sold|generated|does)\b", ql):
            word = re.search(r"\bwhich (\w+)", ql).group(1)  # type: ignore[union-attr]
            if word in SINGULAR_DIMS:
                return 1
        return 10 if group_cols else 100

    def _dim_column(self, word: str) -> ColumnInfo | None:
        role_order = {"categor": ["category"], "region": ["region"], "store": ["store_name", "store_id"],
                      "outlet": ["store_name", "store_id"], "product": ["product_name", "product_id"],
                      "item": ["product_name", "product_id"], "customer": ["customer_id"]}
        key = next((k for k in role_order if word.startswith(k)), None)
        for role in role_order.get(key or "", []):
            cols = sorted(self.catalog.columns_with_role(role), key=lambda c: (self.catalog.datasets[c.dataset].rows_clean, c.column_id))
            if cols:
                return cols[0]
        return None

    def _group_by(self, ql: str, filters: list[Filter]) -> list[str]:
        words = r"(?:categor(?:y|ies)|regions?|stores?|outlets?|products?|items?|customers?)"
        filtered_cols = {f.column_id for f in filters}
        found: list[str] = []
        patterns = [rf"\b(?:by|per|for each|each|across|group(?:ed)? by)\s+(?:the\s+)?({words})(?:\s+and\s+({words}))?",
                    rf"\b(?:top|bottom|best|worst|highest|lowest|which|what)\s+(?:\d+\s+|few\s+)?({words})",
                    rf"\b({words})\s+(?:has|have|had|with|performed|sold|generated)\b"]
        for pat in patterns:
            for m in re.finditer(pat, ql):
                for w in [g for g in m.groups() if g]:
                    col = self._dim_column(w)
                    if col and col.column_id not in found and not self._is_filter_noun(ql, w, col, filters):
                        found.append(col.column_id)
            if found:
                break
        return [c for c in found if c not in filtered_cols or len(found) == 1][:2]

    def _time_dimensions(self, ql: str) -> tuple[list[DateGrouping], list[DateRangeFilter], PlannerOutcome | None]:
        grain = next((g for g in ("month", "quarter", "year", "week", "day")
                      if re.search(rf"\b(?:by|per|each)\s+(?:the\s+)?{g}s?\b", ql)), None)
        wants_range = bool(re.search(r"\blast\s+quarter\b", ql))
        if not grain and not wants_range:
            return [], [], None
        dates = [c for c in self.catalog.all_columns() if c.is_date]
        if not dates:
            return [], [], PlannerOutcome(kind="unanswerable", reason="time queries require a resolved date column",
                                          missing=["date"], source="fallback")
        # A requested date operation must never silently select an ambiguous column.
        candidates = [c for c in dates if not c.date_ambiguous]
        if len(candidates) != 1:
            return [], [], PlannerOutcome(kind="clarify", question="Which resolved date column should I use?",
                                          options=[c.column_id for c in dates], source="fallback")
        col = candidates[0]
        grouping = [DateGrouping(column_id=col.column_id,
                                  grain=cast(Literal["month", "quarter", "year", "week", "day"], grain))] if grain else []
        ranges: list[DateRangeFilter] = []
        if wants_range:
            today = date.today()
            quarter = (today.month - 1) // 3
            year = today.year
            if quarter == 0:
                year -= 1
                quarter = 3
            else:
                quarter -= 1
            start = date(year, quarter * 3 + 1, 1)
            next_start = date(year, quarter * 3 + 4, 1) if quarter < 3 else date(year + 1, 1, 1)
            end = next_start - timedelta(days=1)
            ranges.append(DateRangeFilter(column_id=col.column_id, start=start, end=end))
        return grouping, ranges, None

    def _is_filter_noun(self, ql: str, word: str, col: ColumnInfo, filters: list[Filter]) -> bool:
        """'West region' / 'in the X category' use the noun to qualify a value, not to group by it."""
        return any(f.column_id == col.column_id and f.op == "eq" and re.search(rf"{re.escape(str(f.value).lower())}\s+{word}\b", ql)
                   for f in filters)

    def _filters(self, q: str, ql: str) -> list[Filter]:
        work = normalize_value(q)
        matches: dict[str, list[str]] = {}
        columns = [c for c in self.catalog.all_columns()
                   if c.is_text and not c.is_id and not is_pii_column(c.name) and 0 < c.distinct_count <= 50
                   and c.role in ("category", "region", "store_name", "return_status", None) and c.role != "return_status"]
        for col in columns:
            values = sorted((v for v, _ in self.tools.values(col.column_id, "clean")), key=lambda v: (-len(v), v))
            for value in values:
                nv = normalize_value(value)
                if len(nv) >= 2 and re.search(rf"(?<![\w]){re.escape(nv)}(?![\w])", work):
                    matches.setdefault(col.column_id, []).append(value)
                    work = re.sub(rf"(?<![\w]){re.escape(nv)}(?![\w])", " ", work)
        filters = [Filter(column_id=c, op="eq" if len(v) == 1 else "in",
                          value=cast(Scalar | list[Scalar], v[0] if len(v) == 1 else v))
                   for c, v in sorted(matches.items())]
        taken = {f.column_id for f in filters}
        for token in _ID_TOKEN.findall(q):
            for role in ID_ROLES_FOR_VALUES:
                id_col = next(iter(sorted(self.catalog.columns_with_role(role), key=lambda c: self.catalog.datasets[c.dataset].rows_clean)), None)
                if id_col and id_col.column_id not in taken and any(v.lower() == token.lower() for v, _ in self.tools.values(id_col.column_id, "clean")):
                    filters.append(Filter(column_id=id_col.column_id, op="eq", value=next(v for v, _ in self.tools.values(id_col.column_id, "clean") if v.lower() == token.lower())))
                    taken.add(id_col.column_id)
                    break
        for noun, _role in (("category", "category"), ("region", "region"), ("store", "store_name")):
            dim_col = self._dim_column(noun)
            if dim_col is None or dim_col.column_id in taken:
                continue
            for pat in (rf"\b(?:in|for|of|from)\s+(?:the\s+)?([A-Za-z][\w&\-]*)\s+{noun}\b", rf"\b{noun}\s+(?:is|=|:)?\s*([A-Za-z][\w&\-]*)"):
                m = re.search(pat, q, re.IGNORECASE)
                if m and m.group(1).lower() not in _STOP and m.group(1).lower() not in self._known_words():
                    filters.append(Filter(column_id=dim_col.column_id, op="eq",
                                          value=cast(Scalar, m.group(1))))
                    taken.add(dim_col.column_id)
                    break
        return filters

    def _known_words(self) -> set[str]:
        words = {w for ps in self.cfg["metric_synonyms"].values() for p in ps for w in p.split()}
        return words | {"each", "wise", "level", "name", "id"}

    def _resolve_column(self, phrase: str, numeric: bool = False) -> ColumnInfo | None:
        tokens = [t for t in tokenize_name(phrase) if t not in _STOP]
        if not tokens:
            return None
        roles = load_yaml("roles.yaml")
        joined = "_".join(tokens)
        singular = "_".join(t[:-1] if t.endswith("s") and len(t) > 3 else t for t in tokens)
        for role, spec in roles.items():
            if joined == role or singular == role or joined in [a for a in spec["aliases"]] or singular in spec["aliases"]:
                cols = [c for c in self.catalog.columns_with_role(role) if not numeric or c.is_numeric]
                if cols:
                    return sorted(cols, key=lambda c: c.column_id)[0]
        scored = sorted(((fuzz.ratio(joined, "_".join(tokenize_name(c.name))), c) for c in self.catalog.all_columns()
                         if not numeric or c.is_numeric), key=lambda x: (-x[0], x[1].column_id))
        if scored and scored[0][0] >= 85 and (len(scored) < 2 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    def _special(self, q: str, ql: str, filters: list[Filter], group_cols: list[str]):
        """Generic (non-registry) intents: average/total of a column, counts, distinct, describe."""
        m = re.search(r"\b(average|avg|mean)\s+(?:of\s+)?(?:the\s+)?(.+?)(?:\s+(?:by|per|for|in|across|of)\b|$|\?)", ql)
        if m:
            col = self._resolve_column(m.group(2), numeric=True)
            if col is None:
                return PlannerOutcome(kind="unanswerable", reason=f"could not resolve a numeric column for '{m.group(2).strip()}'", source="fallback")
            return "aggregate", [Aggregation(function="avg", column_id=col.column_id, alias=f"avg_{_slug(col.name)}")], [], group_cols
        m = re.search(r"\b(total|sum of)\s+(?:the\s+)?(.+?)(?:\s+(?:by|per|for|in|across)\b|$|\?)", ql)
        if m:
            col = self._resolve_column(m.group(2), numeric=True)
            if col is None:
                return PlannerOutcome(kind="unanswerable", reason=f"could not resolve a numeric column for '{m.group(2).strip()}'", source="fallback")
            return "aggregate", [Aggregation(function="sum", column_id=col.column_id, alias=f"total_{_slug(col.name)}")], [], group_cols
        m = re.search(r"\b(?:how many|count of|number of)\s+(?:distinct\s+|unique\s+)?(\w+)", ql)
        if m:
            ds = self._dataset_for(m.group(1))
            if ds:
                id_col = next((c for c in self.catalog.datasets[ds].columns.values() if c.is_id), None) \
                    or next(iter(self.catalog.datasets[ds].columns.values()))
                return "aggregate", [Aggregation(function="distinct_count", column_id=id_col.column_id, alias=f"distinct_{ds}")], [], group_cols
        m = re.search(r"\b(?:distinct|unique|list all|list of|show all|list)\s+(?:the\s+)?(\w+)", ql)
        if m:
            col = self._dim_column(m.group(1)) or self._resolve_column(m.group(1))
            if col:
                return "distinct", [], [col.column_id], []
        m = re.search(r"\b(?:describe|summary of|statistics (?:of|for)|profile of)\s+(?:the\s+)?(.+?)(?:\?|$)", ql)
        if m:
            col = self._resolve_column(m.group(1))
            ds = self._dataset_for(m.group(1))
            if col:
                return "describe", [], [col.column_id], []
            if ds:
                return "describe", [], [c.column_id for c in list(self.catalog.datasets[ds].columns.values())[:20] if not is_pii_column(c.name)], []
        return None

    def _dataset_for(self, word: str) -> str | None:
        w = word.lower().rstrip("s")
        return next((d for d in sorted(self.catalog.datasets) if d.lower().rstrip("s") == w), None)

    def _no_metric(self, q: str, ql: str, filters: list[Filter], group_cols: list[str]) -> PlannerOutcome:
        if group_cols and re.search(r"\b(best|top|worst|highest|lowest|performed|performance)\b", ql):
            return PlannerOutcome(kind="clarify", question="Rank by units sold or by gross sales revenue?",
                                  options=["Units sold", "Gross sales revenue"], source="fallback")
        unresolved = [w for w in re.findall(r"[a-z]{4,}", ql) if w not in _STOP][:6]
        return PlannerOutcome(kind="unanswerable", reason="could not map the question to a registered metric, column or dataset",
                              missing=unresolved[:0], source="fallback")

    def _finish(self, plan: QueryPlan, ql: str) -> PlannerOutcome:
        try:
            return PlannerOutcome(kind="plan", plan=self.complete(plan), source="fallback")
        except AppError as exc:
            return PlannerOutcome(kind="unanswerable", reason=exc.message,
                                  missing=(exc.details or {}).get("missing_roles", []), source="fallback")

    def complete(self, plan: QueryPlan) -> QueryPlan:
        """Attach code-resolved relationship ids, then run any evidence tools the plan requires."""
        from app.query.analysis import build_measures, dimension_datasets, fact_datasets
        targets = dimension_datasets(plan) | {c.split(".")[0] for c in plan.select_columns}
        rel_ids: set[str] = set(plan.relationship_ids)
        facts = fact_datasets(build_measures(plan, self.catalog)) or [self._root(plan)]
        for fact in facts:
            if not (targets - {fact}):
                continue
            ids = suggest_relationship_ids(self.catalog.graph, fact, targets)
            if ids:
                rel_ids.update(ids)
            else:  # unreachable via verified edges: surface any direct relationship so the validator reports why
                for t in targets - {fact}:
                    rel_ids.update(r.relationship_id for r in self.catalog.graph.between(fact, t))
        plan.relationship_ids = sorted(rel_ids)
        plan.datasets = sorted({*facts, *targets})
        for issue in missing_evidence(required_evidence(plan, self.catalog), self.tools.log):
            self.tools.run(issue.details["tool"], issue.details["args"], "fallback")
        return plan

    def _root(self, plan: QueryPlan) -> str:
        cols = plan.select_columns or plan.group_by
        return cols[0].split(".")[0] if cols else next(iter(self.catalog.datasets))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "value"
