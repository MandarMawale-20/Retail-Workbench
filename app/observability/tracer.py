"""Per-request trace: ordered events with durations, redacted of secrets."""
from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

_SECRET = re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,}")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class Tracer:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.events: list[dict[str, Any]] = []

    def event(self, component: str, status: str = "ok", duration_ms: int = 0, **fields: Any) -> dict[str, Any]:
        event = {"seq": len(self.events), "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                 "component": component, "status": status, "duration_ms": duration_ms, **redact(fields)}
        self.events.append(event)
        return event

    @contextmanager
    def span(self, component: str, **fields: Any) -> Iterator[dict[str, Any]]:
        start = time.perf_counter()
        extra: dict[str, Any] = {}
        try:
            yield extra
        except BaseException as exc:
            self.event(component, "error", int((time.perf_counter() - start) * 1000),
                       error=str(exc)[:300], **fields, **extra)
            raise
        self.event(component, "ok", int((time.perf_counter() - start) * 1000), **fields, **extra)
