"""Environment-driven settings and versioned YAML configuration."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

CLEANER_VERSION = "cleaner-1.0"
ROLE_CATALOG_VERSION = "roles-1.0"
POLICY_VERSION = "policy-1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "none"
    nvidia_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.3-70b-instruct"
    agnes_api_key: str = ""
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_model: str = "agnes-3.0-flash"
    llm_timeout_s: float = 30
    llm_max_rpm: int = 30
    llm_max_retries: int = 2
    llm_breaker_failures: int = 3
    llm_breaker_cooldown_s: float = 60
    use_narrator: bool = True
    use_advisor: bool = True
    planner_max_input_tokens: int = 6000
    eval_provider: str = "auto"
    api_tokens: str = ""
    api_rate_limit_rpm: int = 120
    job_max_retries: int = 2

    artifacts_dir: Path = Path("./artifacts")
    sqlite_path: Path = Path("./artifacts/meta.sqlite")
    max_upload_mb: int = 100
    max_rows: int = 5_000_000
    query_timeout_s: float = 15
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = Field(default=2, ge=1)

    @property
    def has_nim_key(self) -> bool:
        key = self.nvidia_api_key.strip()
        return bool(key) and "REPLACE_ME" not in key

    @property
    def has_agnes_key(self) -> bool:
        key = self.agnes_api_key.strip()
        return bool(key) and "REPLACE_ME" not in key


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_yaml(name: str) -> Any:
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def thresholds() -> dict[str, Any]:
    return load_yaml("thresholds.yaml")
