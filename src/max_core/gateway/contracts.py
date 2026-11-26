"""Small, immutable, provider-agnostic gateway contracts.

Model replies are *untrusted* proposals. A future MAX Trust layer must authorize tools.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
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


Role = Literal["system", "user", "assistant", "tool"]
StreamKind = Literal["start", "content_delta", "tool_proposal", "usage", "complete"]

_TOOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")


def _default_tool_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def _assert_json_depth(value: object) -> None:
    """Reject oversized recursive structures independent of Python JSON limits.

    Different CPython versions and test runtimes may accept a 3,000-level
    dict before json.dumps reaches their recursion limit. Tool input cannot
    depend on those interpreter-specific thresholds.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > 96:
            raise ValueError("JSON-serializable data exceeds maximum nesting depth")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((item, depth + 1) for item in current)


def _require_utf8(value: str, message: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(message) from None


@dataclass(frozen=True, slots=True, repr=False)
class ToolProposal:
    call_id: str
    name: str
    arguments: dict[str, Any]
    # Not a tool execution result, approval, or a permission grant.


@dataclass(frozen=True, slots=True, repr=False)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=_default_tool_parameters)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _TOOL_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("Invalid tool definition name")
        if not isinstance(self.description, str) or len(self.description) > 4_000:
            raise ValueError("Tool description must be text of at most 4,000 characters")
        _require_utf8(self.description, "Tool description must be valid UTF-8 text")
        if not isinstance(self.parameters, dict):
            raise ValueError("Tool parameters must be a JSON object schema")
        try:
            _assert_json_depth(self.parameters)
            encoded = json.dumps(
                self.parameters, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            encoded_bytes = encoded.encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Tool parameters must be JSON-serializable") from None
        if len(encoded_bytes) > 32_000:
            raise ValueError("Tool parameters schema exceeds 32,000 bytes")


def request_envelope_bytes(
    messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition] = ()
) -> int:
    total = 0
    try:
        for message in messages:
            total += len(message.content.encode("utf-8")) + 32
            if message.tool_call_id is not None:
                total += len(message.tool_call_id.encode("utf-8")) + 16
            for call in message.tool_calls:
                _assert_json_depth(call.arguments)
                encoded = json.dumps(
                    call.arguments, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                )
                total += (
                    len(call.call_id.encode("utf-8"))
                    + len(call.name.encode("utf-8"))
                    + len(encoded.encode("utf-8"))
                    + 16
                )
        for tool in tools:
            _assert_json_depth(tool.parameters)
            encoded = json.dumps(
                tool.parameters, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            total += (
                len(tool.name.encode("utf-8"))
                + len(tool.description.encode("utf-8"))
                + len(encoded.encode("utf-8"))
                + 32
            )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Gateway payload must be JSON-serializable") from None
    return total


MAX_MESSAGE_IMAGES = 2
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_CAPABILITIES = frozenset({"text", "tools", "vision"})


@dataclass(frozen=True, slots=True, repr=False)
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolProposal, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False
    # PNG screenshots for vision models; local-only (see GatewayRequest).
    images: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Unsupported role")
        if not isinstance(self.images, tuple) or len(self.images) > MAX_MESSAGE_IMAGES:
            raise ValueError("images must be a tuple of at most 2 PNG images")
        for image in self.images:
            if (
                not isinstance(image, bytes)
                or not image.startswith(b"\x89PNG\r\n\x1a\n")
                or len(image) > MAX_IMAGE_BYTES
            ):
                raise ValueError("images must be PNG data of at most 8 MB")
        if self.images and self.role != "user":
            raise ValueError("Only user messages may include images")
        if not isinstance(self.content, str) or len(self.content) > 32_000:
            raise ValueError("Message must be text of at most 32,000 characters")
        _require_utf8(self.content, "Message must be valid UTF-8 text")
        if type(self.is_error) is not bool:
            raise ValueError("Tool error flag must be a boolean")
        if not isinstance(self.tool_calls, tuple) or len(self.tool_calls) > 16:
            raise ValueError("tool_calls must be a tuple of at most 16 proposals")
        if self.role == "tool":
            if (
                not isinstance(self.tool_call_id, str)
                or not 1 <= len(self.tool_call_id) <= 128
                or any(ch in self.tool_call_id for ch in ("\r", "\n", "\x00"))
            ):
                raise ValueError("Tool messages require a valid tool_call_id")
            _require_utf8(self.tool_call_id, "Tool messages require a valid tool_call_id")
            if self.tool_calls:
                raise ValueError("Tool messages cannot include tool_calls")
        else:
            if self.tool_call_id is not None or self.is_error:
                raise ValueError("tool_call_id and is_error are only valid for tool messages")
        if self.tool_calls:
            if self.role != "assistant":
                raise ValueError("Only assistant messages may include tool_calls")
            for call in self.tool_calls:
                if not isinstance(call, ToolProposal):
                    raise ValueError("Invalid tool proposal")
                if (
                    not isinstance(call.call_id, str)
                    or not 1 <= len(call.call_id) <= 128
                    or any(ch in call.call_id for ch in ("\r", "\n", "\x00"))
                ):
                    raise ValueError("Invalid tool proposal call_id")
                _require_utf8(call.call_id, "Invalid tool proposal call_id")
                if not isinstance(call.name, str) or _TOOL_NAME_RE.fullmatch(call.name) is None:
                    raise ValueError("Invalid tool proposal name")
                if not isinstance(call.arguments, dict):
                    raise ValueError("Tool proposal arguments must be a JSON object")
                try:
                    _assert_json_depth(call.arguments)
                    encoded = json.dumps(
                        call.arguments,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    encoded_bytes = encoded.encode("utf-8")
                except (TypeError, ValueError, RecursionError):
                    raise ValueError("Tool proposal arguments must be JSON-serializable") from None
                if len(encoded_bytes) > 32_000:
                    raise ValueError("Tool proposal arguments exceed 32,000 bytes")


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
        if type(self.is_local) is not bool:
            raise ValueError("Endpoint locality must be a boolean")
        if not isinstance(self.inference_destination, InferenceDestination):
            raise ValueError("Invalid inference destination classification")
        if self.inference_destination == InferenceDestination.VERIFIED_LOCAL and not self.is_local:
            raise ValueError("Verified local inference requires an explicit loopback endpoint")
        if not self.capabilities or not self.capabilities <= _CAPABILITIES:
            raise ValueError("Only text, inert tool proposals and vision are supported")
        if self.api_key is not None and (
            not self.api_key or "\n" in self.api_key or "\r" in self.api_key
        ):
            raise ValueError("Invalid API key configuration")
        if self.api_key is not None:
            _require_utf8(self.api_key, "Invalid API key configuration")
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
            if rate is not None and (
                not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0
            ):
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
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", self.request_id):
            raise ValueError("Invalid request ID")
        if not self.messages or len(self.messages) > 48:
            raise ValueError("At least one and at most 48 messages are required")
        if not isinstance(self.privacy, Privacy):
            raise ValueError("Unknown data privacy classification")
        if any(
            type(value) is not bool
            for value in (self.cloud_allowed, self.local_only, self.allow_fallback)
        ):
            raise ValueError("Gateway authorization flags must be booleans")
        if (
            not 1 <= self.max_input_tokens <= 1_000_000
            or not 1 <= self.max_output_tokens <= 1_000_000
        ):
            raise ValueError("Invalid token ceilings")
        if not self.required_capabilities or not self.required_capabilities <= _CAPABILITIES:
            raise ValueError("Unsupported requested capabilities")
        if any(m.images for m in self.messages if isinstance(m, ChatMessage)):
            # Screenshots never leave the machine: images need a local-only request
            # to a vision-capable model.
            if not self.local_only or self.cloud_allowed:
                raise ValueError("Images are only allowed in local-only requests")
            if "vision" not in self.required_capabilities:
                raise ValueError("Images require the vision capability")
        if self.max_cost_usd is not None and (
            not isinstance(self.max_cost_usd, Decimal)
            or not self.max_cost_usd.is_finite()
            or self.max_cost_usd < 0
        ):
            raise ValueError("Invalid USD spend ceiling")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Invalid deadline")
        if any(not isinstance(x, ChatMessage) for x in self.messages):
            raise ValueError("Invalid message type")
        if not isinstance(self.tools, tuple) or len(self.tools) > 32:
            raise ValueError("At most 32 tool definitions are supported")
        if any(not isinstance(tool, ToolDefinition) for tool in self.tools):
            raise ValueError("Invalid tool definition")
        tool_names = [tool.name for tool in self.tools]
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("Duplicate tool definition name")
        # Byte count is a deliberately conservative text-only admission estimate.
        # Provider-specific token counting remains unverified; this is not a billing SLA.
        if request_envelope_bytes(self.messages, self.tools) > self.max_input_tokens:
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
