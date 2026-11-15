"""Register immutable raw artifacts and derive dataset version ids."""
from __future__ import annotations

import shutil
from pathlib import Path

from app.common import sha256_file


def register_raw(src: Path, dataset_dir: Path) -> tuple[Path, str, str]:
    """Copy `src` to `<dataset_dir>/raw.csv`; return (path, sha256, dataset_version_id)."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    dst = dataset_dir / "raw.csv"
    shutil.copyfile(src, dst)
    digest = sha256_file(dst)
    return dst, digest, "dv_" + digest[:8]
