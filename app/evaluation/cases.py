"""Tolerant evaluator case loader: lists or {"cases": [...]}, unknown fields preserved, paths resolved."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import AppError

KNOWN = {"case_id", "type", "owner_id", "datasets", "options", "chat", "mock_responses", "expect"}


@dataclass
class Case:
    case_id: str
    type: str
    owner_id: str
    datasets: list[dict[str, str]]
    options: dict[str, Any]
    chat: list[dict[str, Any]]
    mock_responses: list[str] | None
    expect: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    error: AppError | None = None


def load_cases(path: Path) -> list[Case]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("CASE_INPUT_INVALID", f"cannot read cases: {exc}", stage="evaluate") from exc
    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise AppError("CASE_INPUT_INVALID", "cases must be a list or {'cases': [...]}", stage="evaluate")
    return [_parse(i, n, Path(path).resolve().parent) for n, i in enumerate(items)]


def _parse(item: Any, index: int, base: Path) -> Case:
    fallback_id = f"case_{index + 1:03d}"
    if not isinstance(item, dict):
        return Case(fallback_id, "invalid", "eval", [], {}, [], None, {},
                    error=AppError("CASE_INPUT_INVALID", "case must be an object", stage="evaluate"))
    case_id = str(item.get("case_id") or fallback_id)
    try:
        datasets = []
        for d in item.get("datasets") or []:
            path = Path(d["path"])
            datasets.append({"name": d.get("name") or path.stem, "path": str(path if path.is_absolute() else base / path)})
        chat = [{"question": c} if isinstance(c, str) else dict(c) for c in item.get("chat") or []]
        for c in chat:
            if not isinstance(c.get("question"), str):
                raise ValueError("chat turn needs a 'question' string")
        mock = item.get("mock_responses")
        if mock is not None and not (isinstance(mock, list) and all(isinstance(m, str) for m in mock)):
            raise ValueError("mock_responses must be a list of strings")
        kind = item.get("type") or ("pipeline_chat" if chat else "pipeline")
        return Case(case_id, kind, str(item.get("owner_id") or f"eval_{case_id}"), datasets, dict(item.get("options") or {}),
                    chat, mock, dict(item.get("expect") or {}), {k: v for k, v in item.items() if k not in KNOWN})
    except (KeyError, TypeError, ValueError) as exc:
        return Case(case_id, "invalid", "eval", [], {}, [], None, {}, {k: v for k, v in item.items() if k not in KNOWN},
                    error=AppError("CASE_INPUT_INVALID", f"malformed case: {exc}", stage="evaluate"))
