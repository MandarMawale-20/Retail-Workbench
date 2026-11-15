"""`ask()`: the single chat entry point shared by the API and the evaluator."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, cast

from app.agents.fallback_planner import FallbackPlanner
from app.agents.narrator import narrate
from app.agents.planner import LLMPlanner, PlannerOutcome
from app.agents.planner_tools import PlannerTools
from app.common import new_trace_id, sanitize_for_prompt
from app.config import POLICY_VERSION
from app.conversation.clarification import check_clarification, resolve_pending
from app.conversation.context import merge_delta, update_state
from app.conversation.linker import build_schema_context
from app.errors import AppError, as_app_error
from app.evidence.generator import build_evidence
from app.evidence.templates import render_template
from app.observability.tracer import Tracer
from app.query.catalog import Catalog
from app.query.compiler import compile_plan
from app.query.executor import distinct_values, execute_query
from app.query.schemas import (
    ChatResult,
    ConversationState,
    QueryPlan,
    ToolLog,
    ValidationIssue,
    ValidationResult,
)
from app.query.validator import required_evidence, validate_plan
from app.schemas import ExecutionContext
from app.services.context import Services

_REPAIRABLE = {"TOOL_EVIDENCE_MISSING", "UNKNOWN_COLUMN", "INVALID_PLAN", "TYPE_MISMATCH", "UNKNOWN_RELATIONSHIP",
               "SCOPE_VIOLATION", "STAGE_RULE", "LIMIT_EXCEEDED"}
MAX_TURNS_HINT = 12


@dataclass
class QuestionStats:
    """Per-question measurements consumed by the evaluator's per-path metrics."""
    path: str = "fallback"
    planner_source: str = "fallback"
    fallback_used: bool = False
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: int = 0
    tools_used: int = 0
    tool_compliant: bool = True
    plan_valid_first: bool = False
    plan_produced: bool = False
    repaired: bool = False
    invalid_json: bool = False
    narrator_attempted: bool = False
    narrator_accepted: bool = False
    cassette_misses: int = 0
    blocked_at: list[str] = field(default_factory=list)


@dataclass
class AskOutcome:
    result: ChatResult
    stats: QuestionStats
    trace: list[dict]
    tool_log: ToolLog


def create_session(svc: Services, owner_id: str, run_id: str) -> str:
    run = svc.repo.get_run(owner_id, run_id)
    if run["status"] not in ("completed", "completed_with_warnings"):
        raise AppError("UNSUPPORTED_QUERY", f"run is '{run['status']}'; chat needs a completed run", stage="session")
    catalog = Catalog.load(svc.store.run_dir(owner_id, run_id))
    session_id = "sess_" + uuid.uuid4().hex[:12]
    state = ConversationState(session_id=session_id, owner_id=owner_id,
                              active_dataset_version_ids=sorted(catalog.version_ids().values()),
                              graph_version=catalog.graph.graph_version)
    svc.repo.create_session(owner_id, session_id, run_id, state.model_dump())
    return session_id


def reset_session(svc: Services, owner_id: str, session_id: str) -> None:
    session = svc.repo.get_session(owner_id, session_id)
    catalog = Catalog.load(svc.store.run_dir(owner_id, session["run_id"]))
    state = ConversationState(session_id=session_id, owner_id=owner_id,
                              active_dataset_version_ids=sorted(catalog.version_ids().values()),
                              graph_version=catalog.graph.graph_version)
    svc.repo.reset_session(owner_id, session_id, state.model_dump())


def ask(svc: Services, owner_id: str, session_id: str, question: str, *, provider=None, idem_key: str | None = None,
        use_narrator: bool | None = None) -> ChatResult:
    return ask_with_stats(svc, owner_id, session_id, question, provider=provider, idem_key=idem_key,
                          use_narrator=use_narrator).result


def ask_with_stats(svc: Services, owner_id: str, session_id: str, question: str, *, provider=None,
                   idem_key: str | None = None, use_narrator: bool | None = None) -> AskOutcome:
    started = time.perf_counter()
    session = svc.repo.get_session(owner_id, session_id)            # raises ISOLATION_VIOLATION for foreign owners
    if idem_key:
        cached = svc.repo.find_turn_by_idem(owner_id, session_id, idem_key)
        if cached:
            return AskOutcome(ChatResult.model_validate(cached), QuestionStats(), [], ToolLog())
    trace_id = new_trace_id()
    tracer = Tracer(trace_id)
    stats = QuestionStats(path="fallback" if provider is None else "llm")
    state = ConversationState.model_validate(session["state"])
    catalog = Catalog.load(svc.store.run_dir(owner_id, session["run_id"]))
    ctx = ExecutionContext(owner_id=owner_id, session_id=session_id, active_dataset_version_ids=state.active_dataset_version_ids,
                           graph_version=state.graph_version, policy_version=POLICY_VERSION, trace_id=trace_id)
    turn = _Turn(svc, owner_id, state, catalog, ctx, tracer, stats, provider, trace_id,
                 use_narrator if use_narrator is not None else svc.settings.use_narrator)
    calls_before = len(provider.calls) if provider is not None else 0
    try:
        result = turn.run(question)
    except Exception as exc:
        err = as_app_error(exc, "chat")
        tracer.event("final", "error", code=err.code, message=err.message)
        result = ChatResult(status="error", answer_text=f"The question could not be processed: {err.message}",
                            error=err.to_dict(), trace_id=trace_id)
    result.trace_id, result.turn_id = trace_id, "turn_" + uuid.uuid4().hex[:12]
    _fill_provider_stats(stats, provider, calls_before)
    stats.tools_used = len(turn.tool_log.calls)
    stats.latency_ms = int((time.perf_counter() - started) * 1000)
    new_state = turn.new_state(result, question)
    if new_state.turn_count >= MAX_TURNS_HINT and result.status == "ok":
        result.answer_text += "\n(This session is long; you can start a new session to reset filters.)"
    svc.repo.update_session_state(owner_id, session_id, new_state.model_dump())
    svc.repo.save_trace(owner_id, trace_id, tracer.events)
    if result.evidence:
        svc.repo.save_evidence(owner_id, result.evidence.evidence_id, result.evidence.model_dump())
    svc.repo.add_turn(owner_id, {
        "turn_id": result.turn_id, "session_id": session_id, "question": sanitize_for_prompt(question, 500),
        "status": result.status, "plan": result.plan.model_dump() if result.plan else None,
        "validation": result.validation.model_dump() if result.validation else None,
        "execution": {"sql_fingerprint": result.evidence.sql_fingerprint, "rows_considered": result.evidence.rows_considered,
                      "result_row_count": result.evidence.result_row_count} if result.evidence else None,
        "evidence_id": result.evidence.evidence_id if result.evidence else None,
        "answer": result.model_dump(), "trace_id": trace_id}, idem_key)
    return AskOutcome(result, stats, tracer.events, turn.tool_log)


def _fill_provider_stats(stats: QuestionStats, provider, before: int) -> None:
    if provider is None:
        return
    calls = provider.calls[before:]
    stats.llm_calls = len(calls)
    stats.tokens = sum((c.tokens_in or 0) + (c.tokens_out or 0) for c in calls)
    stats.cassette_misses = sum(c.cassette_miss for c in calls)
    sources = [c.source for c in calls if c.ok and c.source]
    stats.path = sources[0] if sources else "fallback"
    stats.path = {"nim_live": "nim_live", "replay": "replay", "mock": "mock", "cache": "nim_live"}.get(stats.path, stats.path)


class _Turn:
    """One question's pipeline; keeps intermediate state so failures still produce a structured result."""

    def __init__(self, svc: Services, owner_id: str, state: ConversationState, catalog: Catalog, ctx: ExecutionContext,
                 tracer: Tracer, stats: QuestionStats, provider, trace_id: str, narrator_on: bool) -> None:
        self.svc, self.owner_id, self.state, self.catalog, self.ctx = svc, owner_id, state, catalog, ctx
        self.tracer, self.stats, self.provider, self.trace_id, self.narrator_on = tracer, stats, provider, trace_id, narrator_on
        self.tool_log = ToolLog()
        self._values: dict[tuple[str, str], list[tuple[str, int]]] = {}
        self.pending: dict | None = None
        self.final_plan: QueryPlan | None = None
        self.settings = svc.settings

    def values(self, column_id: str, stage: str) -> list[tuple[str, int]]:
        key = (column_id, stage)
        if key not in self._values:
            self._values[key] = distinct_values(self.catalog, self.settings, column_id, stage)
        return self._values[key]

    def new_state(self, result: ChatResult, question: str) -> ConversationState:
        state = update_state(self.state, self.final_plan if result.status == "ok" else None,
                             result.evidence.evidence_id if result.evidence else None, question)
        state.pending_clarification = self.pending
        return state

    # ------------------------------------------------------------------ flow
    def run(self, question: str) -> ChatResult:
        question = question.strip()
        if not question:
            return self._refuse("The question is empty.", "INVALID_QUERY_PLAN")
        if len(question) > 500:
            question = question[:500]
        self.tracer.event("question", text=sanitize_for_prompt(question, 500))
        resolved = resolve_pending(question, self.state.pending_clarification)
        if resolved:
            self.tracer.event("clarification_resolved", resolved=resolved)
            question = resolved
        clar = check_clarification(question, self.catalog, self.state)
        if clar:
            return self._clarify(clar, question)
        tools = PlannerTools(self.catalog, self.values, self.tool_log)
        outcome = self._plan(question, tools)
        if outcome.kind == "clarify":
            return self._clarify({"kind": "planner", "question": outcome.question, "options": outcome.options}, question)
        if outcome.kind == "unanswerable":
            return self._unsupported(outcome.reason or "unanswerable", outcome.missing)
        if outcome.kind == "failed":
            return self._refuse(outcome.error.message if outcome.error else "planning failed", "INVALID_QUERY_PLAN")

        plan = self._to_plan(outcome)
        validation = self._validate(plan, first=True)
        planner = outcome.source == "llm"
        if not validation.valid and planner and outcome.repairs_used < 1 and any(e.code in _REPAIRABLE for e in validation.errors):
            self.stats.repaired = True
            repaired = self._llm.repair(outcome, [e for e in validation.errors if e.code in _REPAIRABLE]) if planner else outcome
            self._absorb(outcome, repaired)
            if repaired.kind in ("plan", "delta"):
                plan = self._to_plan(repaired)
                validation = self._validate(plan)
            elif repaired.kind in ("clarify", "unanswerable"):
                return (self._clarify({"kind": "planner", "question": repaired.question, "options": repaired.options}, question)
                        if repaired.kind == "clarify" else self._unsupported(repaired.reason or "", repaired.missing))
        if not validation.valid:
            return self._from_validation(plan, validation, question)
        return self._execute(question, plan, validation)

    def _plan(self, question: str, tools: PlannerTools) -> PlannerOutcome:
        if self.provider is not None:
            try:
                ctx = build_schema_context(question, self.catalog, self.state, max_tokens=self.settings.planner_max_input_tokens)
            except AppError as exc:
                return PlannerOutcome(kind="unanswerable", reason=exc.message, source="llm")
            tools.default_limit = ctx.list_distinct_limit
            self._llm = LLMPlanner(self.provider, tools, self.tracer, self.trace_id)
            outcome = self._llm.plan(question, ctx, self.state.recent_questions)
            self.tracer.event("planner", outcome.kind, source="llm", llm_calls=outcome.llm_calls, trim=ctx.trim_log,
                              repairs=outcome.repairs_used, blocked=outcome.blocked_at, flagged_values=tools.flagged)
            self.stats.invalid_json = outcome.invalid_json > 0
            self.stats.blocked_at = list(outcome.blocked_at)
            self.stats.planner_source = "llm"
            if outcome.kind != "failed":
                return outcome
            self.stats.fallback_used = True
            self.tracer.event("fallback", "ok", reason=outcome.error.code if outcome.error else "unknown")
        outcome = FallbackPlanner(self.catalog, tools, self.state).plan(question)
        self.tracer.event("planner", outcome.kind, source="fallback")
        self.stats.planner_source = "fallback"
        return outcome

    def _absorb(self, before: PlannerOutcome, after: PlannerOutcome) -> None:
        self.stats.invalid_json = self.stats.invalid_json or after.invalid_json > 0
        self.stats.blocked_at = list(dict.fromkeys([*self.stats.blocked_at, *after.blocked_at]))

    def _to_plan(self, outcome: PlannerOutcome) -> QueryPlan:
        if outcome.kind == "delta":
            plan = merge_delta(self.state.previous_plan, outcome.delta or [])
            if outcome.source == "fallback":
                plan = FallbackPlanner(self.catalog, PlannerTools(self.catalog, self.values, self.tool_log), self.state).complete(plan)
            return plan
        assert outcome.plan is not None
        return outcome.plan

    def _validate(self, plan: QueryPlan, first: bool = False) -> ValidationResult:
        validation = validate_plan(plan, self.ctx, self.catalog, self.tool_log, self.values)
        self.stats.plan_produced = True
        if first:
            self.stats.plan_valid_first = validation.valid
            self.stats.tool_compliant = not any(e.code == "TOOL_EVIDENCE_MISSING" for e in validation.errors)
        self.tracer.event("validation", "ok" if validation.valid else "invalid", errors=[e.code for e in validation.errors],
                          required=required_evidence(plan, self.catalog).model_dump(),
                          tool_calls=[{"tool": c.tool, "args": c.args, "source": c.source} for c in self.tool_log.calls])
        self.final_plan = plan
        return validation

    def _execute(self, question: str, plan: QueryPlan, validation: ValidationResult) -> ChatResult:
        compiled = compile_plan(plan, self.catalog)
        self.tracer.event("compile", fingerprint=compiled.fingerprint)
        result = execute_query(compiled, self.catalog, self.settings)
        self.tracer.event("execute", rows_considered=result.rows_considered, rows=result.result_row_count,
                          duration_ms=result.duration_ms)
        evidence = build_evidence(plan, validation, compiled, result, self.catalog, self.ctx, self.trace_id)
        self.tracer.event("facts", count=len(evidence.facts))
        text, source = render_template(plan, evidence, compiled.columns, result.rows, len(compiled.grain)), "template"
        if self.provider is not None and self.narrator_on and plan.intent in ("aggregate", "top_n", "compare"):
            narration = narrate(self.provider, question, evidence, self.trace_id)
            self.stats.narrator_attempted, self.stats.narrator_accepted = narration.attempted, narration.accepted
            self.tracer.event("narrator", "ok" if narration.accepted else "rejected", reasons=narration.reasons)
            if narration.accepted:
                text, source = f"{narration.text}\n\n{text}", "narrator"
        self.tracer.event("final", "ok", answer_source=source)
        return ChatResult(status="ok", answer_text=text,
                          answer_source=cast(Literal["template", "narrator", "none"], source),
                          plan=plan, validation=validation, evidence=evidence)

    # ------------------------------------------------------------------ non-ok results
    def _clarify(self, clar: dict, question: str) -> ChatResult:
        self.pending = {**clar, "original": question}
        self.tracer.event("final", "clarification_required", kind=clar.get("kind"))
        options = clar.get("options") or []
        text = clar["question"] + (f" Options: {', '.join(options)}." if options else "") + (f" {clar['note']}" if clar.get("note") else "")
        return ChatResult(status="clarification_required", answer_text=text, clarification=clar)

    def _unsupported(self, reason: str, missing: list[str]) -> ChatResult:
        self.tracer.event("final", "unsupported", missing=missing)
        text = f"I can't answer that from the available data: {reason}."
        if missing:
            text += f" Missing roles: {', '.join(missing)}."
        return ChatResult(status="unsupported", answer_text=text,
                          error=AppError("UNSUPPORTED_QUERY", reason, stage="plan", details={"missing_roles": missing}).to_dict())

    def _refuse(self, message: str, code: str = "INVALID_QUERY_PLAN", details: dict | None = None) -> ChatResult:
        self.tracer.event("final", "refused", code=code)
        return ChatResult(status="refused", answer_text=f"I did not run this query: {message}",
                          error=AppError(code, message, stage="validate", details=details).to_dict())  # type: ignore[arg-type]

    def _from_validation(self, plan: QueryPlan, validation: ValidationResult, question: str) -> ChatResult:
        errors = validation.errors
        by_code: dict[str, ValidationIssue] = {}
        for e in errors:
            by_code.setdefault(e.code, e)
        res = self._map_errors(by_code, question)
        res.plan, res.validation = plan, validation
        return res

    def _map_errors(self, by_code: dict[str, ValidationIssue], question: str) -> ChatResult:
        if "AMBIGUOUS_DATE" in by_code:
            col = by_code["AMBIGUOUS_DATE"].message.split("'")[1]
            return self._clarify({"kind": "date_format", "column_id": col, "options": ["DD/MM/YYYY", "MM/DD/YYYY"],
                                  "question": by_code["AMBIGUOUS_DATE"].message + " Which format does it use?",
                                  "note": "Approve the skipped parse_date_iso step (interactive_review) to enable date questions."}, question)
        if "VALUE_NOT_FOUND" in by_code:
            e = by_code["VALUE_NOT_FOUND"]
            near = e.details.get("near", [])
            return self._clarify({"kind": "value", "options": near, "column_id": e.details.get("column_id"),
                                  "question": f"{e.message}." + (f" Did you mean {', '.join(near)}?" if near else
                                                                " No close values exist.")}, question)
        if "UNSUPPORTED_METRIC" in by_code:
            e = by_code["UNSUPPORTED_METRIC"]
            ambiguous = e.details.get("ambiguous_roles", [])
            if ambiguous:
                options = [str(item) for item in ambiguous]
                return self._clarify({
                    "kind": "role_binding",
                    "options": options,
                    "question": "Several columns could satisfy this metric. Which role binding should I use?",
                    "note": "Reply with the preferred column or dataset.",
                }, question)
            return self._unsupported(e.message, e.details.get("missing_roles", []))
        if "PII_COLUMN" in by_code:
            return self._refuse(by_code["PII_COLUMN"].message, "INVALID_QUERY_PLAN", {"issues": ["PII_COLUMN"]})
        if "UNSAFE_JOIN" in by_code:
            e = by_code["UNSAFE_JOIN"]
            return self._refuse(e.message + ". No safe join exists for this question, so no query was run.", "UNSAFE_JOIN", e.details)
        e = next(iter(by_code.values()))
        code = "UNKNOWN_RELATIONSHIP" if e.code == "UNKNOWN_RELATIONSHIP" else "INVALID_QUERY_PLAN"
        return self._refuse(f"{e.message} (check: {e.path})", code, {"issues": [x.code for x in by_code.values()]})
