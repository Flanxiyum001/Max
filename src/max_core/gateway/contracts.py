"""Small, immutable, provider-agnostic gateway contracts.

Model replies are *untrusted* proposals. A future MAX Trust layer must authorize tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit


class Dialect(StrEnum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class Privacy(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class InferenceDestination(StrEnum):
    """Owner-verified inference egress, never inferred from the HTTP host.

    VERIFIED_LOCAL is a trusted configuration assertion, NOT runtime attestation.
    UNKNOWN is the default and is never eligible for a gateway request.
    """

    VERIFIED_LOCAL = "verified_local"
    CLOUD = "cloud"
    UNKNOWN = "unknown"


Role = Literal["system", "user", "assistant"]
StreamKind = Literal["start", "content_delta", "tool_proposal", "usage", "complete"]


@dataclass(frozen=True, slots=True, repr=False)
class ChatMessage:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported role")
        if not isinstance(self.content, str) or len(self.content) > 32_000:
            raise ValueError("Message must be text of at most 32,000 characters")


@dataclass(frozen=True, slots=True, repr=False)
class ModelSpec:
    provider_id: str
    model_id: str
    dialect: Dialect
    endpoint: str
    is_local: bool
    context_tokens: int
    inference_destination: InferenceDestination = InferenceDestination.UNKNOWN
    capabilities: frozenset[str] = frozenset({"text"})
    region: str = "unspecified"
    priority: int = 100
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.provider_id):
            raise ValueError("Invalid provider identifier")
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,150}", self.model_id):
            raise ValueError("Invalid model identifier")
        if not isinstance(self.dialect, Dialect) or self.context_tokens < 1:
            raise ValueError("Invalid model dialect or context window")
        if not isinstance(self.inference_destination, InferenceDestination):
            raise ValueError("Invalid inference destination classification")
        if self.inference_destination == InferenceDestination.VERIFIED_LOCAL and not self.is_local:
            raise ValueError("Verified local inference requires an explicit loopback endpoint")
        if not self.capabilities or not self.capabilities <= {"text", "tools"}:
            raise ValueError("Only text and inert tool proposals are supported in milestone 3")
        if self.api_key is not None and (
            not self.api_key or "\n" in self.api_key or "\r" in self.api_key
        ):
            raise ValueError("Invalid API key configuration")
        parts = urlsplit(self.endpoint)
        hostname = parts.hostname or ""
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not hostname
            or not parts.path.startswith("/")
            or parts.path == "/"
        ):
            raise ValueError("Gateway endpoint must be an explicit, credential-free API URL")
        if self.is_local:
            if parts.scheme not in {"http", "https"} or hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Local model endpoints must use an explicit loopback host")
        elif parts.scheme != "https" or hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote model endpoints require HTTPS and a non-loopback host")
        for rate in (self.input_usd_per_million, self.output_usd_per_million):
            if rate is not None and (not rate.is_finite() or rate < 0):
                raise ValueError("Invalid model token pricing")


@dataclass(frozen=True, slots=True, repr=False)
class GatewayRequest:
    request_id: str
    messages: tuple[ChatMessage, ...]
    privacy: Privacy
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: Decimal | None = None
    cloud_allowed: bool = False
    local_only: bool = False
    required_capabilities: frozenset[str] = frozenset({"text"})
    allowed_models: frozenset[str] = frozenset()
    allowed_providers: frozenset[str] = frozenset()
    allowed_regions: frozenset[str] = frozenset()
    allow_fallback: bool = False
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", self.request_id):
            raise ValueError("Invalid request ID")
        if not self.messages or len(self.messages) > 48:
            raise ValueError("At least one and at most 48 messages are required")
        if not isinstance(self.privacy, Privacy):
            raise ValueError("Unknown data privacy classification")
        if (
            not 1 <= self.max_input_tokens <= 1_000_000
            or not 1 <= self.max_output_tokens <= 1_000_000
        ):
            raise ValueError("Invalid token ceilings")
        if not self.required_capabilities or not self.required_capabilities <= {"text", "tools"}:
            raise ValueError("Unsupported requested capabilities")
        if self.max_cost_usd is not None and (
            not self.max_cost_usd.is_finite() or self.max_cost_usd < 0
        ):
            raise ValueError("Invalid USD spend ceiling")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Invalid deadline")
        if any(not isinstance(x, ChatMessage) for x in self.messages):
            raise ValueError("Invalid message type")
        # Byte count is a deliberately conservative text-only admission estimate.
        # Provider-specific token counting remains unverified; this is not a billing SLA.
        if (
            sum(len(message.content.encode("utf-8")) + 32 for message in self.messages)
            > self.max_input_tokens
        ):
            raise ValueError("Declared input-token ceiling is below conservative text envelope")
        seen_non_system = False
        for message in self.messages:
            if message.role == "system" and seen_non_system:
                raise ValueError("System messages must precede conversation turns")
            if message.role != "system":
                seen_non_system = True


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True, repr=False)
class ToolProposal:
    call_id: str
    name: str
    arguments: dict[str, Any]
    # Not a tool execution result, approval, or a permission grant.


@dataclass(frozen=True, slots=True, repr=False)
class GatewayResponse:
    request_id: str
    provider_id: str
    model_id: str
    content: str
    tool_proposals: tuple[ToolProposal, ...]
    finish_reason: str | None
    usage: TokenUsage | None
    estimated_cost_usd: Decimal | None


@dataclass(frozen=True, slots=True, repr=False)
class StreamEvent:
    sequence: int
    request_id: str
    provider_id: str
    model_id: str
    kind: StreamKind
    text: str = ""
    tool_proposal: ToolProposal | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


class GatewayError(Exception):
    """Sanitized operational failure; no provider payloads, URLs or secret material."""

    def __init__(self, code: str, *, fallback_safe: bool = False) -> None:
        self.code = code
        self.fallback_safe = fallback_safe
        super().__init__(code)
