"""Bootstrap configuration (pydantic-settings). Only the essentials to start up."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INCANT_", env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./incant.db"
    repo_path: str = "./var/repo"          # canonical git repository (bare)

    # Serving
    default_environment: str = "prod"
    mode: str = "full"                     # full | serve

    # Bind
    host: str = "0.0.0.0"
    port: int = 8080

    # Auth: a bootstrap admin key so the instance is usable out of the box.
    bootstrap_admin_key: str = "incant_sk_dev_admin"

    def repo_dir(self) -> Path:
        return Path(self.repo_path).resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override settings (tests)."""
    global _settings
    _settings = settings
