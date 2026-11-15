"""Deterministic hashing, id helpers and text sanitisation used across the code base."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
INJECTION_PATTERNS = re.compile(
    r"ignore (all |the )?(previous|prior|above|rules)|system\s*:|disregard|you are now|reveal .*prompt|"
    r"owner_id|drop table|run sql|<\s*/?\s*data\s*>",
    re.IGNORECASE,
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def h(*parts: Any) -> str:
    """Canonical content hash (hex sha256)."""
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex[:16]


def sanitize_name(raw: str) -> str:
    """Logical dataset names are restricted to `[a-z0-9_]+`."""
    stem = Path(raw).stem.lower()
    name = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    return name or "dataset"


def normalize_header(name: str) -> str:
    """Normalised column name: trimmed, whitespace collapsed (case is preserved)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(name))).strip()


def tokenize_name(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()) if t]


def sanitize_for_prompt(value: Any, max_len: int = 64) -> str:
    """NFKC-normalise, strip control characters and truncate untrusted cell-derived text."""
    text = unicodedata.normalize("NFKC", str(value))
    text = _CONTROL.sub("", text).replace("\n", " ").replace("\r", " ")
    return text[:max_len]


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION_PATTERNS.search(text))


def fence_data(payload: Any) -> str:
    """Wrap untrusted structured data so a prompt can label it as data, never instructions."""
    body = canonical_json(payload).replace("</data>", "<\\/data>")
    return f"<data>{body}</data>"


def normalize_value(value: Any) -> str:
    """Case/whitespace-insensitive comparison key for categorical values."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()
