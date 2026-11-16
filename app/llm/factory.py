"""Provider selection for `--provider auto|replay|mock|fallback|openrouter`."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.llm.base import LLMProvider, MeteredProvider
from app.llm.cassette_provider import AutoProvider, CassetteProvider
from app.llm.mock_provider import MockProvider
from app.llm.openrouter_provider import CachingProvider, OpenRouterProvider
from app.storage.repositories import Repository


def build_provider(mode: str, settings: Settings, repo: Repository, prompt_version: str, *, case_id: str = "case",
                   cassette_dir: Path | None = None, record_dir: Path | None = None,
                   mock_responses: list[str] | None = None) -> MeteredProvider | None:
    use_openrouter = mode == "openrouter"
    model = settings.openrouter_model
    cassette = (Path(cassette_dir) / f"{case_id}.jsonl") if cassette_dir else None

    def live() -> LLMProvider | None:
        if not use_openrouter and mode not in {"auto", "replay"}:
            return None
        if not settings.has_openrouter_key:
            return None
        provider = OpenRouterProvider(settings)
        return CachingProvider(provider, repo, prompt_version, model)

    inner: LLMProvider | None
    if mode == "fallback":
        return None
    if mock_responses is not None or mode == "mock":
        inner = MockProvider(mock_responses or [])
    elif mode == "replay":
        inner = CassetteProvider(cassette or Path("/nonexistent.jsonl"), mode="replay", prompt_version=prompt_version,
                                 model=model, model_aliases=[settings.openrouter_model, "synthetic-emulator"])
    elif mode == "openrouter":
        inner = live()
        if inner is not None and record_dir:
            inner = CassetteProvider(Path(record_dir) / f"{case_id}.jsonl", mode="record", prompt_version=prompt_version,
                                     model=model, inner=inner)
    elif mode == "auto":
        replay = CassetteProvider(cassette, mode="replay", prompt_version=prompt_version, model=model,
                                  model_aliases=[settings.openrouter_model, "synthetic-emulator"]) \
            if cassette and cassette.exists() else None
        live_provider = live()
        inner = AutoProvider(replay, live_provider) if (replay or live_provider) else None
    else:
        raise ValueError(f"unknown provider mode '{mode}'")
    return MeteredProvider(inner) if inner is not None else None
