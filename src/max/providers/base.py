"""Provider abstraction: canonical messages in, text/tool-calls out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """Base class for provider failures that must never crash the service."""

    code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.detail = message

    @property
    def public_message(self) -> str:
        """Message safe to show to the user / store in a session."""
        return f"model provider error ({self.code})"


class ProviderConfigError(ProviderError):
    code = "provider_config"


class ProviderTimeout(ProviderError):
    code = "timeout"
    retryable = True


class ProviderAuthError(ProviderError):
    code = "auth"
    retryable = False


class ProviderRateLimit(ProviderError):
    code = "rate_limited"
    retryable = True


class ProviderServerError(ProviderError):
    code = "server_error"
    retryable = True


class ProviderProtocolError(ProviderError):
    code = "bad_response"
    retryable = False


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ChatProvider(ABC):
    """A chat-completions style model provider."""

    name = "provider"

    @abstractmethod
    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ProviderResult:
        """Run one completion. Must raise ProviderError on failure."""

    def health(self) -> dict:
        return {"provider": self.name, "ok": True}
