"""Filesystem artifact store with path containment and content hashes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.common import canonical_json, sha256_file
from app.ingestion.loader import ensure_within


def safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", value)[:64] or "_"


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, owner_id: str, run_id: str) -> Path:
        path = self.root / safe_segment(owner_id) / safe_segment(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return ensure_within(path, self.root)

    def write_json(self, path: Path, obj: Any) -> str:
        ensure_within(path, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(json.loads(canonical_json(obj)), indent=2, sort_keys=True, ensure_ascii=False),
                        encoding="utf-8")
        return sha256_file(path)

    def read_json(self, path: Path) -> Any:
        return json.loads(ensure_within(path, self.root).read_text(encoding="utf-8"))
