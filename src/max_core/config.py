"""Explicit, fail-closed configuration for the local bootstrap server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """An unsafe or incomplete startup configuration (never contains secret values)."""


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    """Phase 1 bootstrap intentionally supports only a loopback listener."""

    bearer_token: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    log_level: str = "INFO"
    database_url: str | None = None

    def __post_init__(self) -> None:
        token = self.bearer_token
        if len(token) < 32 or any(char.isspace() for char in token) or len(set(token)) < 12:
            raise ConfigurationError(
                "MAX_API_BEARER_TOKEN must be a strong random value of at least 32 characters"
            )
        if self.bind_host != "127.0.0.1":
            raise ConfigurationError("MAX_BIND_HOST must be 127.0.0.1 in this milestone")
        if not 1024 <= self.bind_port <= 65535:
            raise ConfigurationError("MAX_BIND_PORT must be an integer between 1024 and 65535")
        if self.database_url is not None and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ConfigurationError("MAX_DATABASE_URL must use postgresql+psycopg://")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("MAX_LOG_LEVEL must be a supported logging level")

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> Settings:
        """Read only named configuration keys; never print environment contents."""
        env = os.environ if source is None else source
        raw_port = env.get("MAX_BIND_PORT", "8765")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError("MAX_BIND_PORT must be an integer") from exc
        return cls(
            bearer_token=env.get("MAX_API_BEARER_TOKEN", ""),
            bind_host=env.get("MAX_BIND_HOST", "127.0.0.1"),
            bind_port=port,
            log_level=env.get("MAX_LOG_LEVEL", "INFO").upper(),
            database_url=env.get("MAX_DATABASE_URL") or None,
        )
