"""Scripted provider for tests and `mock_responses` evaluator cases."""
from __future__ import annotations

from app.errors import AppError
from app.llm.base import Kind, LLMResponse

_SIMULATED = {
    "timeout": AppError("LLM_TIMEOUT", "simulated timeout", retryable=True, stage="llm"),
    "rate_limited": AppError("LLM_RATE_LIMITED", "simulated 429", retryable=True, stage="llm"),
}


class MockProvider:
    """Returns ordered canned responses; `SIMULATE:timeout|rate_limited` entries raise provider errors."""
    name = "mock"

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.index = 0
        self.requests: list[list[dict]] = []

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        self.requests.append(messages)
        if self.index >= len(self.responses):
            raise AppError("LLM_TIMEOUT", "mock responses exhausted", retryable=True, stage="llm")
        text = self.responses[self.index]
        self.index += 1
        if text.startswith("SIMULATE:"):
            raise _SIMULATED[text.split(":", 1)[1]]
        return LLMResponse(text=text, model="mock", tokens_in=0, tokens_out=0, source="mock")
