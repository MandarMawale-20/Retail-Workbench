"""Cassette record/replay of LLM responses. Keys hash the full request; bodies are never stored."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from app.common import h
from app.errors import AppError
from app.llm.base import Kind, LLMProvider, LLMResponse

_SIMULATED = {
    "timeout": ("LLM_TIMEOUT", "simulated timeout (cassette)"),
    "rate_limited": ("LLM_RATE_LIMITED", "simulated 429 (cassette)"),
}


def request_key(kind: str, prompt_version: str, model: str, messages: list[dict]) -> str:
    return h(kind, prompt_version, model, messages)


class CassetteProvider:
    """Replay (no network) or record (wraps a live provider) one case's `.jsonl` cassette."""

    def __init__(self, path: Path, *, mode: str, prompt_version: str, model: str, inner: LLMProvider | None = None,
                 model_aliases: list[str] | None = None) -> None:
        assert mode in ("replay", "record")
        self.path, self.mode, self.version, self.model, self.inner = Path(path), mode, prompt_version, model, inner
        # Replay accepts configured model aliases so deterministic fixtures remain usable after model changes.
        self.models = [model, *(m for m in model_aliases or [] if m != model)]
        self.name = mode
        self.hits = self.misses = 0
        self._queues: dict[str, list[dict]] = defaultdict(list)
        self._seq = 0
        if mode == "replay" and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._queues[entry["key"]].append(entry)
        elif mode == "record":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.unlink(missing_ok=True)

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        if self.mode == "record":
            assert self.inner is not None
            key = request_key(kind, self.version, self.model, messages)
            try:
                resp = self.inner.complete_json(kind=kind, messages=messages, max_tokens=max_tokens, trace_id=trace_id)
            except AppError as exc:
                sim = "rate_limited" if exc.code == "LLM_RATE_LIMITED" else "timeout"
                self._append({"key": key, "kind": kind, "seq": self._seq, "simulate": sim, "model": self.model})
                raise
            self._append({"key": key, "kind": kind, "seq": self._seq, "response_text": resp.text, "model": resp.model,
                          "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "latency_ms": resp.latency_ms,
                          "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")})
            return resp
        queue = next((q for m in self.models if (q := self._queues.get(request_key(kind, self.version, m, messages)))), None)
        if not queue:
            self.misses += 1
            raise AppError("LLM_TIMEOUT", "no cassette entry for this request (stale or missing recording)",
                           retryable=False, stage="llm", details={"cassette_miss": True, "kind": kind})
        entry = queue.pop(0) if len(queue) > 1 else queue[0]
        self.hits += 1
        if entry.get("simulate"):
            code, message = _SIMULATED[entry["simulate"]]
            raise AppError(code, message, retryable=True, stage="llm")  # type: ignore[arg-type]
        return LLMResponse(text=entry["response_text"], model=entry.get("model", self.model),
                           tokens_in=entry.get("tokens_in"), tokens_out=entry.get("tokens_out"),
                           latency_ms=entry.get("latency_ms") or 0, source="replay")

    def _append(self, entry: dict) -> None:
        self._seq += 1
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


class AutoProvider:
    """Per request: cassette hit -> replay; else live provider if configured; else an LLM-unavailable error."""
    name = "auto"

    def __init__(self, replay: CassetteProvider | None, live: LLMProvider | None) -> None:
        self.replay, self.live = replay, live
        self.cassette_misses = 0

    def complete_json(self, *, kind: Kind, messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse:
        if self.replay is not None:
            try:
                return self.replay.complete_json(kind=kind, messages=messages, max_tokens=max_tokens, trace_id=trace_id)
            except AppError as exc:
                if not (exc.details or {}).get("cassette_miss"):
                    raise
                self.cassette_misses += 1
        if self.live is None:
            raise AppError("LLM_TIMEOUT", "no LLM available (cassette miss and no OPENROUTER_API_KEY)", stage="llm",
                           details={"cassette_miss": self.replay is not None})
        return self.live.complete_json(kind=kind, messages=messages, max_tokens=max_tokens, trace_id=trace_id)
