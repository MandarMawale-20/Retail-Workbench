"""Upload validation: extension, size, name sanitisation, encoding detection and path containment."""
from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes
from pydantic import BaseModel

from app.common import sanitize_name
from app.errors import AppError

_UTF8_ALIASES = {"utf_8", "utf-8", "ascii", "utf_8_sig", "utf-8-sig"}


class InputFile(BaseModel):
    logical_name: str
    path: Path


def ensure_within(path: Path, root: Path) -> Path:
    """Resolve `path` and verify it stays under `root` (blocks traversal and symlink escapes)."""
    resolved, base = path.resolve(), root.resolve()
    if not resolved.is_relative_to(base):
        raise AppError("INVALID_FILE", "path escapes the allowed root", stage="ingest")
    return resolved


def validate_file(path: Path, logical_name: str | None, *, max_upload_mb: int) -> InputFile:
    """Validate one staged file and return its sanitised logical identity."""
    path = Path(path)
    if path.suffix.lower() != ".csv":
        raise AppError("INVALID_FILE", f"only .csv files are accepted (got '{path.suffix}')", stage="ingest")
    if not path.is_file():
        raise AppError("INVALID_FILE", "file does not exist", stage="ingest")
    size = path.stat().st_size
    if size == 0:
        raise AppError("INVALID_FILE", "file is empty", stage="ingest")
    if size > max_upload_mb * 1024 * 1024:
        raise AppError("INVALID_FILE", f"file exceeds {max_upload_mb} MB limit", stage="ingest")
    with path.open("rb") as fh:
        if b"\x00" in fh.read(65536):
            raise AppError("INVALID_FILE", "binary content detected", stage="ingest")
    return InputFile(logical_name=sanitize_name(logical_name or path.name), path=path)


def detect_encoding(path: Path) -> str:
    """Best-effort encoding detection on a bounded sample."""
    with path.open("rb") as fh:
        sample = fh.read(1 << 20)
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    best = from_bytes(sample).best()
    return (best.encoding if best else "latin-1") or "latin-1"


def _normalise_newlines(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def to_utf8(src: Path, dst: Path) -> str:
    """Write a UTF-8, LF-terminated staging copy of `src` at `dst`; return the detected source encoding.

    The staged copy is only used for parsing; the registered raw artifact keeps the original bytes.
    """
    encoding = detect_encoding(src)
    data = src.read_bytes()
    if encoding.replace("_", "-").lower() in _UTF8_ALIASES:
        data = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
        dst.write_bytes(_normalise_newlines(data))
        return "utf-8"
    text = data.decode(encoding, errors="replace")
    dst.write_bytes(_normalise_newlines(text.encode("utf-8")))
    return encoding
