"""Typed application errors shared by every layer."""
from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal[
    "INVALID_FILE", "SCHEMA_INFERENCE_FAILED", "CLEANING_VALIDATION_FAILED", "UNKNOWN_RELATIONSHIP",
    "UNSAFE_JOIN", "INVALID_QUERY_PLAN", "UNSUPPORTED_QUERY", "CLARIFICATION_REQUIRED", "QUERY_TIMEOUT",
    "LLM_TIMEOUT", "LLM_RATE_LIMITED", "INVALID_LLM_JSON", "ISOLATION_VIOLATION", "INTERNAL_ERROR",
    "CASE_INPUT_INVALID",
]


class AppError(Exception):
    """Structured error: `{code, message, retryable, stage, details}`."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, stage: str = "unknown",
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.stage = stage
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "stage": self.stage, "details": self.details}


def as_app_error(exc: BaseException, stage: str) -> AppError:
    """Wrap any exception so callers always deal with a structured error."""
    if isinstance(exc, AppError):
        return exc
    return AppError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", retryable=False, stage=stage)
