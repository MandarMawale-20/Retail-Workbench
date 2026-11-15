"""Run state machine: only allowed transitions, failures never end in `completed`."""
from __future__ import annotations

from app.errors import AppError

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"ingesting", "failed"},
    "ingesting": {"profiling", "failed"},
    "profiling": {"planning", "failed"},
    "planning": {"cleaning", "needs_review", "failed"},
    "needs_review": {"cleaning", "failed"},
    "cleaning": {"validating", "failed"},
    "validating": {"completed", "completed_with_warnings", "failed"},
    "completed": set(),
    "completed_with_warnings": set(),
    "failed": set(),
}


class RunStateMachine:
    def __init__(self, on_change=None) -> None:
        self.state = "queued"
        self.history = ["queued"]
        self._on_change = on_change

    def advance(self, new_state: str) -> None:
        if new_state not in TRANSITIONS[self.state]:
            raise AppError("INTERNAL_ERROR", f"illegal run transition {self.state} -> {new_state}", stage="manager")
        self.state = new_state
        self.history.append(new_state)
        if self._on_change:
            self._on_change(new_state)
