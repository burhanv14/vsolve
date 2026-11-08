"""Runtime configuration for the issue_agent MCP server.

All settings are read from environment variables (loaded from ``.env`` when
present) via :mod:`pydantic-settings`. The GitHub token is never logged or
returned to the host.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    PydanticBaseSettingsSource,
)


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Only read configuration from .env (plus explicit init kwargs). We
    # deliberately do NOT read from the OS environment, because system env
    # vars such as PATH would collide with our config fields (e.g. `path`).
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, dotenv_settings

    # --- GitHub -----------------------------------------------------------
    github_token: str = Field(default="", description="GitHub personal access token")
    github_api_base_url: str = Field(
        default="https://api.github.com", description="GitHub REST API base URL"
    )

    # --- Workspace / server ----------------------------------------------
    workspace_root: str = Field(
        default="./workspaces",
        description="Directory under which all per-issue workspaces live",
    )
    host: str = Field(default="127.0.0.1", description="HTTP bind host (127.0.0.1 only)")
    port: int = Field(default=8765, description="HTTP bind port")
    path: str = Field(default="/mcp", description="Streamable HTTP endpoint path")

    # --- Guardrails -------------------------------------------------------
    max_diff_lines: int = Field(
        default=500, description="Max diff lines allowed in a single commit"
    )
    clone_timeout_seconds: int = Field(default=300)
    command_timeout_seconds: int = Field(default=300)
    max_output_bytes: int = Field(default=50_000)

    @field_validator("host")
    @classmethod
    def _host_must_be_loopback(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("HOST must be 127.0.0.1 (loopback only); never 0.0.0.0")
        return v

    @field_validator("workspace_root")
    @classmethod
    def _workspace_root_must_be_absolute(cls, v: str) -> str:
        from pathlib import Path

        p = Path(v)
        if not p.is_absolute():
            p = Path.cwd() / p
        return str(p.resolve())


def load_config() -> Config:
    """Load configuration from env / .env."""
    return Config()


# Module-level singleton for convenience.
config = load_config()
