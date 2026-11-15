"""Shared service container (settings, repository, artifact store)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.storage.artifacts import ArtifactStore
from app.storage.database import Database
from app.storage.repositories import Repository


@dataclass
class Services:
    settings: Settings
    repo: Repository
    store: ArtifactStore

    @classmethod
    def create(cls, settings: Settings | None = None, *, artifacts_dir: Path | None = None,
               sqlite_path: Path | None = None) -> Services:
        settings = settings or get_settings()
        root = Path(artifacts_dir or settings.artifacts_dir)
        db = Database(sqlite_path or (root / "meta.sqlite"))
        return cls(settings=settings, repo=Repository(db), store=ArtifactStore(root))
