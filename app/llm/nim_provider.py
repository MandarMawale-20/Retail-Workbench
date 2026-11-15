"""NVIDIA NIM provider (OpenAI-compatible) with own limiter, backoff, circuit breaker and cache layer."""
from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from typing import Any

from app.common import h
from app.config import Settings
from app.errors import AppError
from app.llm.base import CircuitBreaker, Kind, LLMResponse, TokenBucket
from app.storage.repositories import Repository

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class _Retryable(Exception):
    def __init__(self, error: AppError, retry_after: float | None = None) -> None:
        super().__init__(error.message)
        self.error, self.retry_after = error, retry_after


def classify(exc: Exception) -> _Retryable | AppError:
    """Map SDK/transport exceptions onto structured errors (retryable or not)."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return _Retryable(AppError("LLM_TIMEOUT", "LLM request timed out", retryable=True, stage="llm"))
    if status == 429:
        retry_after = None
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            retry_after = float(str(headers.get("retry-after"))) if headers.get("retry-after") else None
        except (TypeError, ValueError):
            retry_after = None
        return _Retryable(AppError("LLM_RATE_LIMITED", "LLM rate limited (429)", retryable=True, stage="llm"), retry_after)
    if isinstance(status, int) and status >= 500:
        return _Retryable(AppError("LLM_TIMEOUT", f"LLM server error {status}", retryable=True, stage="llm"))
    if status in (401, 403):
        return AppError("INTERNAL_ERROR", "NIM authentication failed: check NVIDIA_API_KEY", stage="llm")
    if status == 404:
        return AppError("INTERNAL_ERROR", "NIM model or endpoint not found: check NIM_BASE_URL (/v1) and NIM_MODEL", stage="llm")
    if "Connection" in name:
        return _Retryable(AppError("LLM_TIMEOUT", "LLM connection failed", retryable=True, stage="llm"))
    return AppError("INTERNAL_ERROR", f"LLM call failed: {name}", stage="llm")


class NimProvider:
    name = "nim"

    def __init__(self, settings: Settings, client: Any | None = None, *, limiter: TokenBucket | None = None,
                 breaker: CircuitBreaker | None = None, sleep: Callable[[float], None] = time.sleep,
                 base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 provider_name: str = "nim") -> None:
        self.settings = settings
        self.model = model or settings.nim_model
        self.name = provider_name
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=base_url or settings.nim_base_url, api_key=api_key or settings.nvidia_api_key,
                            timeout=settings.llm_timeout_s, max_retries=0)
        self.client = client
        self.limiter = limiter or TokenBucket(settings.llm_max_rpm)
        self.breaker = breaker or CircuitBreaker(settings.llm_breaker_failures, settings.llm_breaker_cooldown_s)
        self.sleep = sleep

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        if not self.breaker.allow():
            raise AppError("LLM_TIMEOUT", "LLM circuit breaker is open", retryable=True, stage="llm")
        attempts = self.settings.llm_max_retries + 1
        last: AppError | None = None
        for attempt in range(attempts):
            self.limiter.acquire()
            start = time.perf_counter()
            try:
                raw = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0,  # type: ignore[arg-type]
                                                          max_tokens=max_tokens)
            except Exception as exc:
                mapped = classify(exc)
                if isinstance(mapped, AppError):
                    self.breaker.record_failure()
                    raise mapped from exc
                last = mapped.error
                if attempt + 1 < attempts:
                    delay = mapped.retry_after if mapped.retry_after is not None else min(8.0, 0.5 * 2 ** attempt) + random.random() * 0.25
                    self.sleep(delay)
                continue
            self.breaker.record_success()
            text = _THINK.sub("", raw.choices[0].message.content or "").strip()
            usage = getattr(raw, "usage", None)
            return LLMResponse(text=text, model=self.model, tokens_in=getattr(usage, "prompt_tokens", None),
                               tokens_out=getattr(usage, "completion_tokens", None),
                               latency_ms=int((time.perf_counter() - start) * 1000),
                               source=f"{self.name}_live")
        self.breaker.record_failure()
        assert last is not None
        raise last


class AgnesProvider(NimProvider):
    """Agnes AI's OpenAI-compatible chat-completions provider."""

    def __init__(self, settings: Settings, client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(settings, client, base_url=settings.agnes_base_url, api_key=settings.agnes_api_key,
                         model=settings.agnes_model, provider_name="agnes", **kwargs)


class CachingProvider:
    """Cache layer keyed by kind + prompt version + model + canonical messages."""

    def __init__(self, inner, repo: Repository, prompt_version: str, model: str) -> None:
        self.inner, self.repo, self.version, self.model = inner, repo, prompt_version, model
        self.name = getattr(inner, "name", "llm")

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        key = h(kind, self.version, self.model, messages)
        hit = self.repo.llm_cache_get(key)
        if hit:
            return LLMResponse(text=hit["response_text"], model=hit["model"], cached=True, source="cache")
        resp = self.inner.complete_json(kind=kind, messages=messages, max_tokens=max_tokens, trace_id=trace_id)
        self.repo.llm_cache_put(key, resp.text, resp.model)
        return resp
