"""Provider selection for `--provider auto|replay|mock|fallback|nim|agnes`."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.llm.base import LLMProvider, MeteredProvider
from app.llm.cassette_provider import AutoProvider, CassetteProvider
from app.llm.mock_provider import MockProvider
from app.llm.nim_provider import AgnesProvider, CachingProvider, NimProvider
from app.storage.repositories import Repository


def build_provider(mode: str, settings: Settings, repo: Repository, prompt_version: str, *, case_id: str = "case",
                   cassette_dir: Path | None = None, record_dir: Path | None = None,
                   mock_responses: list[str] | None = None) -> MeteredProvider | None:
    use_agnes = mode == "agnes"
    # Replay cassettes may have been recorded by either OpenAI-compatible
    # provider; use the configured live provider's model for key generation.
    replay_agnes = mode == "replay" and settings.llm_provider.lower() == "agnes"
    model = settings.agnes_model if (use_agnes or replay_agnes) else settings.nim_model
    cassette = (Path(cassette_dir) / f"{case_id}.jsonl") if cassette_dir else None

    def live() -> LLMProvider | None:
        if use_agnes and not settings.has_agnes_key:
            return None
        if not use_agnes and not settings.has_nim_key:
            return None
        provider = AgnesProvider(settings) if use_agnes else NimProvider(settings)
        return CachingProvider(provider, repo, prompt_version, model)

    inner: LLMProvider | None
    if mode == "fallback":
        return None
    if mock_responses is not None or mode == "mock":
        inner = MockProvider(mock_responses or [])
    elif mode == "replay":
        inner = CassetteProvider(cassette or Path("/nonexistent.jsonl"), mode="replay", prompt_version=prompt_version,
                                 model=model, model_aliases=[settings.nim_model, settings.agnes_model])
    elif mode == "nim":
        inner = live()
        if inner is not None and record_dir:
            inner = CassetteProvider(Path(record_dir) / f"{case_id}.jsonl", mode="record", prompt_version=prompt_version,
                                     model=model, inner=inner)
    elif mode == "agnes":
        inner = live()
        if inner is not None and record_dir:
            inner = CassetteProvider(Path(record_dir) / f"{case_id}.jsonl", mode="record", prompt_version=prompt_version,
                                     model=model, inner=inner)
    elif mode == "auto":
        replay = CassetteProvider(cassette, mode="replay", prompt_version=prompt_version, model=model,
                                  model_aliases=[settings.nim_model, settings.agnes_model]) \
            if cassette and cassette.exists() else None
        live_provider = live()
        inner = AutoProvider(replay, live_provider) if (replay or live_provider) else None
    else:
        raise ValueError(f"unknown provider mode '{mode}'")
    return MeteredProvider(inner) if inner is not None else None
