"""LLM planner: bounded inspect -> plan loop (<=2 rounds, <=4 tools, <=1 repair) over a JSON protocol."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.agents.planner_tools import PlannerTools
from app.agents.prompts import extract_first_json, planner_messages, repair_message, tool_results_message
from app.conversation.linker import SchemaContext
from app.errors import AppError
from app.observability.tracer import Tracer
from app.query.schemas import ALLOWED_TOOLS, PlanDelta, PlannerOutput, QueryPlan, ValidationIssue

MAX_TOOL_CALLS = 4


@dataclass
class PlannerOutcome:
    kind: str                                  # plan | delta | clarify | unanswerable | failed
    plan: QueryPlan | None = None
    delta: list[PlanDelta] | None = None
    question: str | None = None
    options: list[str] = field(default_factory=list)
    reason: str | None = None
    missing: list[str] = field(default_factory=list)
    source: str = "llm"                        # llm | fallback
    llm_calls: int = 0
    repairs_used: int = 0
    invalid_json: int = 0
    blocked_at: list[str] = field(default_factory=list)
    error: AppError | None = None
    messages: list[dict] = field(default_factory=list)
    last_text: str = ""


class LLMPlanner:
    def __init__(self, provider, tools: PlannerTools, tracer: Tracer, trace_id: str, max_tokens: int = 900) -> None:
        self.provider, self.tools, self.tracer, self.trace_id, self.max_tokens = provider, tools, tracer, trace_id, max_tokens

    # ------------------------------------------------------------------ public
    def plan(self, question: str, ctx: SchemaContext, recent_questions: list[str]) -> PlannerOutcome:
        out = PlannerOutcome(kind="failed", messages=planner_messages(question, ctx.payload, recent_questions))
        parsed = self._call(out)
        if parsed is None:
            return out
        if parsed.type == "tool_call":
            results = self._run_tools(parsed, out)
            out.messages += [{"role": "assistant", "content": out.last_text}, tool_results_message(results, out.blocked_at)]
            parsed = self._call(out)
            if parsed is None:
                return out
            if parsed.type == "tool_call":
                out.kind, out.error = "failed", AppError("INVALID_LLM_JSON", "planner requested tools twice", stage="planner")
                return out
        return self._finalize(out, parsed)

    def repair(self, out: PlannerOutcome, issues: list[ValidationIssue]) -> PlannerOutcome:
        """The single repair: execute any missing evidence tools ourselves and ask for a revised plan."""
        if out.repairs_used >= 1:
            return out
        out.repairs_used += 1
        results = [self.tools.run(i.details["tool"], i.details["args"], "code_injected")
                   for i in issues if i.code == "TOOL_EVIDENCE_MISSING"]
        errors = [{"code": i.code, "path": i.path, "message": i.message, "suggestion": i.suggestion} for i in issues]
        out.messages += [{"role": "assistant", "content": out.last_text}, repair_message(json.dumps(errors)[:1500], results or None)]
        out.kind, out.error, out.plan, out.delta = "failed", None, None, None
        parsed = self._call(out, allow_parse_repair=False)
        return out if parsed is None else self._finalize(out, parsed)

    # ------------------------------------------------------------------ internals
    def _run_tools(self, parsed: PlannerOutput, out: PlannerOutcome) -> list[dict]:
        allowed = [c for c in parsed.calls if c.tool in ALLOWED_TOOLS][:MAX_TOOL_CALLS]
        blocked = [c.tool for c in parsed.calls if c.tool not in ALLOWED_TOOLS]
        if blocked or len(parsed.calls) > MAX_TOOL_CALLS:
            out.blocked_at.append("tool_allowlist")
        self.tracer.event("planner_tools", requested=[c.tool for c in parsed.calls], blocked=blocked)
        return [self.tools.run(c.tool, c.args, "llm_requested") for c in allowed]

    def _call(self, out: PlannerOutcome, allow_parse_repair: bool = True) -> PlannerOutput | None:
        for _ in range(2):
            try:
                resp = self.provider.complete_json(kind="planner", messages=out.messages, max_tokens=self.max_tokens,
                                                   trace_id=self.trace_id)
            except AppError as exc:
                out.kind, out.error = "failed", exc
                return None
            out.llm_calls += 1
            out.last_text = resp.text
            try:
                return PlannerOutput.model_validate(extract_first_json(resp.text))
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                out.invalid_json += 1
                out.blocked_at.append("planner_output_schema")
                self.tracer.event("planner_parse", "error", error=str(exc)[:200])
                if not allow_parse_repair or out.repairs_used >= 1:
                    out.kind, out.error = "failed", AppError("INVALID_LLM_JSON", "planner output invalid after repair", stage="planner")
                    return None
                out.repairs_used += 1
                out.messages += [{"role": "assistant", "content": resp.text[:2000]}, repair_message(str(exc)[:400])]
        return None

    def _finalize(self, out: PlannerOutcome, parsed: PlannerOutput) -> PlannerOutcome:
        out.error = None
        if parsed.type == "final_plan" and parsed.plan:
            out.kind, out.plan = "plan", parsed.plan
        elif parsed.type == "final_delta" and parsed.delta:
            out.kind, out.delta = "delta", parsed.delta
        elif parsed.type == "clarify" and parsed.question:
            out.kind, out.question, out.options = "clarify", parsed.question, parsed.options
        elif parsed.type == "unanswerable":
            out.kind, out.reason, out.missing = "unanswerable", parsed.reason or "unanswerable", parsed.missing
        else:
            out.kind, out.error = "failed", AppError("INVALID_LLM_JSON", f"unusable planner output '{parsed.type}'", stage="planner")
        return out
