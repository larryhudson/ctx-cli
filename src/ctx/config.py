"""Configuration management for ctx."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_config_dir() -> Path:
    """Get the config directory path."""
    return Path.home() / ".config" / "ctx"


def get_data_dir() -> Path:
    """Get the data directory path."""
    return Path.home() / ".local" / "share" / "ctx"


class DatabaseConfig(BaseModel):
    """Database configuration."""

    path: Path = Field(default_factory=lambda: get_data_dir() / "chroma_data")


class EmbeddingConfig(BaseModel):
    """Embedding model configuration."""

    model: Literal["default", "openai"] = "default"
    openai_api_key: str | None = None


class SlackConfig(BaseModel):
    """Slack configuration."""

    token: str | None = None  # xoxc token
    cookie: str | None = None  # xoxd cookie
    channels: list[str] = Field(default_factory=list)


class LinearConfig(BaseModel):
    """Linear configuration."""

    api_key: str | None = None


class GitHubConfig(BaseModel):
    """GitHub configuration."""

    token: str | None = None
    repos: list[str] = Field(default_factory=list)


class NotionConfig(BaseModel):
    """Notion configuration."""

    token: str | None = None
    root_pages: list[str] = Field(default_factory=list)
    user_id: str | None = None  # For involvement detection
    user_name: str | None = None  # For mention detection


class ObsidianConfig(BaseModel):
    """Obsidian configuration."""

    vault_path: Path | None = None
    include_folders: list[str] = Field(default_factory=list)


class Config(BaseSettings):
    """Main configuration container."""

    model_config = SettingsConfigDict(
        env_prefix="CTX_",
        env_nested_delimiter="__",
    )

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file and environment variables.

    Priority (highest to lowest):
    1. Environment variables
    2. Config file
    3. Defaults
    """
    config_data: dict = {}

    # Try to load from file
    if config_path is None:
        config_path = get_config_dir() / "config.toml"

    if config_path.exists():
        with config_path.open("rb") as f:
            config_data = tomllib.load(f)

    # Build config from file data, then environment overrides
    return Config(**config_data)


# Global config instance (lazy loaded)
_config: Config | None = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config  # noqa: PLW0603
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset the global config (for testing)."""
    global _config  # noqa: PLW0603
    _config = None
