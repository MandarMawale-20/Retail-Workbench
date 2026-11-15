"""Synthetic LLM emulator used ONLY to generate committed cassettes without a live key.

It answers the planner protocol by running the deterministic fallback planner and rendering its decisions as the JSON
protocol (tool_call -> final_plan/final_delta/clarify/unanswerable). The real planner loop, validator, repair and
claim verifier therefore run on the replayed path, but the responses are NOT produced by a real model.
Re-record with `--provider nim --record-cassettes` to replace them with genuine NVIDIA NIM output.
"""
from __future__ import annotations

import json
import re

from app.agents.fallback_planner import FallbackPlanner
from app.agents.planner_tools import PlannerTools
from app.conversation.context import merge_delta
from app.errors import AppError
from app.llm.base import Kind, LLMResponse
from app.query.catalog import Catalog
from app.query.executor import distinct_values
from app.query.schemas import ConversationState, ToolLog
from app.services.context import Services


class SessionRef:
    session_id: str | None = None
    run_id: str | None = None


class SyntheticProvider:
    name = "synthetic"

    def __init__(self, svc: Services, owner_id: str, ref: SessionRef) -> None:
        self.svc, self.owner_id, self.ref = svc, owner_id, ref

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        text = self._narrate(messages) if kind == "narrator" else self._plan(messages)
        return LLMResponse(text=text, model="synthetic-emulator", tokens_in=len(json.dumps(messages)) // 4,
                           tokens_out=len(text) // 4, latency_ms=0, source="synthetic")

    def _plan(self, messages: list[dict]) -> str:
        user = next(m["content"] for m in messages if m["role"] == "user")
        match = re.search(r"Question: <data>(.*?)</data>", user, re.DOTALL)
        question = json.loads(match.group(1)) if match else ""
        has_results = any("Tool results:" in m["content"] for m in messages if m["role"] == "user")
        assert self.ref.session_id and self.ref.run_id
        session = self.svc.repo.get_session(self.owner_id, self.ref.session_id)
        state = ConversationState.model_validate(session["state"])
        catalog = Catalog.load(self.svc.store.run_dir(self.owner_id, self.ref.run_id))
        log = ToolLog()
        tools = PlannerTools(catalog, lambda c, s: distinct_values(catalog, self.svc.settings, c, s), log)
        outcome = FallbackPlanner(catalog, tools, state).plan(question)
        if outcome.kind in ("plan", "delta"):
            planner = FallbackPlanner(catalog, tools, state)
            merged = merge_delta(state.previous_plan, outcome.delta or []) if outcome.kind == "delta" else None
            completed = planner.complete(merged) if merged is not None else None   # logs the evidence tools it needs
            if outcome.kind == "delta":
                assert completed is not None
                completed_plan = completed
            else:
                assert outcome.plan is not None
                completed_plan = None
            if not has_results and log.calls:
                calls = [{"tool": c.tool, "args": c.args} for c in log.calls][:4]
                return json.dumps({"type": "tool_call", "calls": calls}, sort_keys=True)
            if outcome.kind == "plan":
                plan = outcome.plan
                assert plan is not None
                return json.dumps({"type": "final_plan", "plan": plan.model_dump(exclude_defaults=True)}, sort_keys=True)
            deltas = [d.model_dump(exclude_defaults=True) for d in outcome.delta or []]
            assert completed_plan is not None
            deltas[0]["relationship_ids"] = sorted(completed_plan.relationship_ids)   # a competent model names the joins it needs
            return json.dumps({"type": "final_delta", "delta": deltas}, sort_keys=True)
        if outcome.kind == "clarify":
            return json.dumps({"type": "clarify", "question": outcome.question, "options": outcome.options}, sort_keys=True)
        return json.dumps({"type": "unanswerable", "reason": outcome.reason or "unanswerable", "missing": outcome.missing}, sort_keys=True)

    def _narrate(self, messages: list[dict]) -> str:
        user = next(m["content"] for m in messages if m["role"] == "user")
        match = re.search(r"Facts: <data>(.*?)</data>", user, re.DOTALL)
        facts = json.loads(match.group(1)) if match else []
        value_facts = [f for f in facts if not f["name"].startswith(("diff_top2", "pct_diff", "max|", "min|", "rows_considered", "result_rows"))]
        if not value_facts:
            raise AppError("LLM_TIMEOUT", "synthetic narrator has no facts", stage="llm")
        label = value_facts[0]["name"].split("|")[0]
        lead = f"{label} leads with " if label != "overall" and not re.search(r"\d", label) and len(label) <= 40 else "The result is "
        segments = [{"type": "text", "text": lead}, {"type": "fact", "fact_id": value_facts[0]["id"]}, {"type": "text", "text": "."}]
        return json.dumps({"segments": segments}, sort_keys=True)
