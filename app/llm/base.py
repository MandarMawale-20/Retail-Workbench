"""LLM provider interface, response model, call metering and resilience primitives."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel

from app.errors import AppError

Kind = Literal["planner", "advisor", "narrator"]


class LLMResponse(BaseModel):
    text: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    cached: bool = False
    source: str = ""          # replay | openrouter_live | mock | cache


class LLMProvider(Protocol):
    name: str

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse: ...


class CallRecord(BaseModel):
    kind: str
    ok: bool
    source: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    error_code: str | None = None
    cassette_miss: bool = False


class MeteredProvider:
    """Records every call so the evaluator can report per-question calls, tokens and latency."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "llm")
        self.calls: list[CallRecord] = []

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        start = time.perf_counter()
        try:
            resp = self.inner.complete_json(kind=kind, messages=messages, max_tokens=max_tokens, trace_id=trace_id)
        except AppError as exc:
            self.calls.append(CallRecord(kind=kind, ok=False, error_code=exc.code,
                                         latency_ms=int((time.perf_counter() - start) * 1000),
                                         cassette_miss=bool((exc.details or {}).get("cassette_miss"))))
            raise
        self.calls.append(CallRecord(kind=kind, ok=True, source=resp.source, tokens_in=resp.tokens_in,
                                     tokens_out=resp.tokens_out, latency_ms=resp.latency_ms or
                                     int((time.perf_counter() - start) * 1000)))
        return resp


class TokenBucket:
    """Client-side rate limiter: at most `rpm` requests per minute (smooth refill)."""

    def __init__(self, rpm: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.capacity = max(rpm, 1)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self.clock, self.sleep = clock, sleep
        self.updated = clock()

    def acquire(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            wait = (1 - self.tokens) / self.rate
            self.sleep(wait)
            self.tokens, self.updated = 0.0, self.clock()
        else:
            self.tokens -= 1


class CircuitBreaker:
    def __init__(self, failures: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.cooldown, self.clock = failures, cooldown_s, clock
        self.consecutive = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.cooldown:
            self.opened_at, self.consecutive = None, self.threshold - 1  # half-open: one probe
            return True
        return False

    def record_success(self) -> None:
        self.consecutive, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.consecutive += 1
        if self.consecutive >= self.threshold:
            self.opened_at = self.clock()
