"""Multi-turn cloud assistant chat coordinator for MAX.

Integrates ``DesktopConfig`` / ``ProviderConfig`` (M1), ``Gateway`` (M3/M9),
``with_persona`` (M20), ``privacy_guard`` (M16), and bounded tool execution
with fail-closed error sanitization and thread-safe state management.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from uuid import uuid4

import httpx

from max_core.gateway.adapters import (
    HTTPAdapter,
    _chunked_scan_messages,
    _walk_strings,
    request_has_high_confidence_secret,
)
from max_core.gateway.contracts import (
    _TOOL_NAME_RE,
    ChatMessage,
    Dialect,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
    ModelSpec,
    Privacy,
    ToolDefinition,
    ToolProposal,
    request_envelope_bytes,
)
from max_core.gateway.privacy_guard import contains_high_confidence_secret
from max_core.gateway.router import Gateway
from max_core.local_config import DesktopConfig, ProviderConfig
from max_core.persona import PersonaError, ReplyMode, persona_message, with_persona
from max_core.tool_recovery import ToolFailureGuard

ToolStatus = Literal["completed", "awaiting_approval", "denied", "failed"]

_MAX_HISTORY_MESSAGES_CAP = 46
_MAX_PRE_PERSONA_MESSAGES = 47
_MAX_USER_INPUT_CHARS = 32_000
_MAX_CHAT_MESSAGE_CHARS = 32_000
_MESSAGE_TRUNCATION_SUFFIX = "\n...[truncated]"
_MAX_TOOL_RESULT_CHARS = 28_000
_TOOL_RESULT_TRUNCATE_CHARS = 20_000
_INPUT_ENVELOPE_MARGIN_BYTES = 64
_MIN_OUTPUT_TOKEN_FLOOR = 256
_WIRE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WIRE_DISALLOWED_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True, slots=True)
class ToolExecutionSummary:
    """Host-recorded outcome for a single model-proposed tool invocation."""

    call_id: str
    tool_name: str
    arguments: dict[str, object]
    status: ToolStatus
    output: dict[str, object] | str
    approval_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    """Sanitized result of a single assistant chat turn or approval resume."""

    ok: bool
    reply: str
    provider: str | None
    model_id: str | None
    error_code: str | None
    tool_executions: tuple[ToolExecutionSummary, ...] = ()
    estimated_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Secret-free summary of the currently configured cloud LLM provider."""

    configured: bool
    provider: str | None
    model_id: str | None


class ToolExecutor(Protocol):
    """Trusted host execution boundary for model-proposed tool calls."""

    def tool_definitions(self) -> Sequence[ToolDefinition]: ...

    def execute(
        self,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
    ) -> ToolExecutionSummary: ...


@dataclass(frozen=True, slots=True)
class _ToolNameCodec:
    """Bijective mapping between canonical MAX tool names and provider-safe wire names."""

    wire_tools: tuple[ToolDefinition, ...]
    canonical_to_wire: dict[str, str]
    wire_to_canonical: dict[str, str]

    @classmethod
    def build(cls, tools: Sequence[ToolDefinition]) -> _ToolNameCodec:
        deduped: list[ToolDefinition] = []
        seen_canonical: set[str] = set()
        for tool in tools:
            if not isinstance(tool, ToolDefinition):
                raise ValueError("invalid_tool_definition")
            if tool.name not in seen_canonical:
                seen_canonical.add(tool.name)
                deduped.append(tool)

        if len(deduped) > 32:
            raise ValueError("too_many_tools")

        canonical_to_wire: dict[str, str] = {}
        wire_to_canonical: dict[str, str] = {}

        # First reserve exact wire-compatible names to avoid unnecessary renaming.
        for tool in deduped:
            if _WIRE_TOOL_NAME_RE.fullmatch(tool.name) and tool.name not in wire_to_canonical:
                canonical_to_wire[tool.name] = tool.name
                wire_to_canonical[tool.name] = tool.name

        for tool in deduped:
            if tool.name in canonical_to_wire:
                continue
            sanitized = _WIRE_DISALLOWED_CHARS_RE.sub("_", tool.name).strip("_")
            if not sanitized:
                sanitized = "tool"
            elif not (sanitized[0].isalpha() or sanitized[0] == "_"):
                sanitized = f"t_{sanitized}"
            base = sanitized[:64]
            candidate = base
            if (
                candidate in wire_to_canonical
                or not _WIRE_TOOL_NAME_RE.fullmatch(candidate)
                or _TOOL_NAME_RE.fullmatch(candidate) is None
            ):
                digest = hashlib.sha256(tool.name.encode("utf-8")).hexdigest()[:6]
                counter = 0
                while True:
                    suffix = f"_{digest}" if counter == 0 else f"_{digest}_{counter}"
                    prefix_max = max(1, 64 - len(suffix))
                    candidate = f"{base[:prefix_max]}{suffix}"
                    if (
                        candidate not in wire_to_canonical
                        and _WIRE_TOOL_NAME_RE.fullmatch(candidate)
                        and _TOOL_NAME_RE.fullmatch(candidate) is not None
                    ):
                        break
                    counter += 1
            canonical_to_wire[tool.name] = candidate
            wire_to_canonical[candidate] = tool.name

        wire_tools = tuple(
            ToolDefinition(
                name=canonical_to_wire[tool.name],
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in deduped
        )
        return cls(
            wire_tools=wire_tools,
            canonical_to_wire=canonical_to_wire,
            wire_to_canonical=wire_to_canonical,
        )

    def to_canonical(self, wire_name: str) -> str:
        return self.wire_to_canonical.get(wire_name, wire_name)


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    approval_id: str
    wire_proposal: ToolProposal
    canonical_proposal: ToolProposal
    remaining_proposals: tuple[ToolProposal, ...]
    turn_messages: tuple[ChatMessage, ...]
    prior_summaries: tuple[ToolExecutionSummary, ...]
    placeholder_group: tuple[ChatMessage, ...]
    codec: _ToolNameCodec
    spent_cost: Decimal | None


_GATEWAY_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "auth_failure": (
        "auth_failed",
        "Authentication with the configured AI provider failed. Check your API key.",
    ),
    "rate_limited": (
        "rate_limited",
        "The AI provider rate limit was reached. Please wait a moment and try again.",
    ),
    "provider_unavailable": (
        "provider_unavailable",
        "The AI provider is temporarily unavailable. Please try again shortly.",
    ),
    "timeout": (
        "network_error",
        "The request to the AI provider timed out. Check your connection and try again.",
    ),
    "stream_interrupted": (
        "network_error",
        "The connection to the AI provider was interrupted. Please try again.",
    ),
    "cloud_sensitive_content_blocked": (
        "egress_blocked",
        "Message blocked because it appears to contain sensitive credentials or secrets.",
    ),
    "budget_exceeded": (
        "budget_exceeded",
        "Request exceeds the configured per-turn USD spend ceiling.",
    ),
    "invalid_request": (
        "invalid_request",
        "The chat request could not be accepted by the provider.",
    ),
    "model_mismatch": (
        "model_mismatch",
        "The AI provider returned an unexpected model identifier.",
    ),
    "malformed_provider_response": (
        "provider_error",
        "The AI provider returned a malformed response.",
    ),
    "invalid_tool_proposal": (
        "provider_error",
        "The AI provider returned an invalid tool proposal.",
    ),
    "response_too_large": (
        "provider_error",
        "The AI provider response exceeded the allowed size limit.",
    ),
    "unsupported_capability": (
        "provider_error",
        "The configured model does not support the requested capability.",
    ),
    "no_eligible_provider": (
        "provider_error",
        "No eligible AI provider is available for this request.",
    ),
    "provider_error": (
        "provider_error",
        "The AI provider returned an error while processing the request.",
    ),
}


def _to_decimal(value: float | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("non_numeric_value")
    num = float(value)
    if not math.isfinite(num) or num < 0.0:
        raise ValueError("invalid_numeric_value")
    try:
        dec = Decimal(str(num))
    except InvalidOperation:
        raise ValueError("invalid_numeric_value") from None
    if not dec.is_finite() or dec < 0:
        raise ValueError("invalid_numeric_value")
    return dec


def _build_model_spec(
    provider_cfg: ProviderConfig,
    *,
    has_tools: bool,
) -> tuple[ModelSpec, Decimal]:
    input_rate = _to_decimal(provider_cfg.input_usd_per_million)
    output_rate = _to_decimal(provider_cfg.output_usd_per_million)
    max_cost = _to_decimal(provider_cfg.max_cost_usd)

    if provider_cfg.provider == "openai":
        dialect = Dialect.OPENAI_CHAT
        context_tokens = 128_000
    elif provider_cfg.provider == "anthropic":
        dialect = Dialect.ANTHROPIC_MESSAGES
        context_tokens = 200_000
    else:
        raise ValueError("unsupported_provider")

    capabilities = frozenset({"text", "tools"}) if has_tools else frozenset({"text"})
    spec = ModelSpec(
        provider_id=provider_cfg.provider,
        model_id=provider_cfg.model_id,
        dialect=dialect,
        endpoint=provider_cfg.endpoint,
        is_local=False,
        context_tokens=context_tokens,
        inference_destination=InferenceDestination.CLOUD,
        capabilities=capabilities,
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
        api_key=provider_cfg.api_key,
    )
    return spec, max_cost


def _bound_message_text(text: str, limit: int = _MAX_CHAT_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_MESSAGE_TRUNCATION_SUFFIX):
        return text[:limit]
    return f"{text[: limit - len(_MESSAGE_TRUNCATION_SUFFIX)]}{_MESSAGE_TRUNCATION_SUFFIX}"


def _safe_str(value: object) -> str | None:
    try:
        return str(value)
    except (TypeError, ValueError, RecursionError):
        return None


def _serialize_tool_output(status: ToolStatus, output: dict[str, object] | str) -> str:
    payload: dict[str, object] = {"status": status, "output": output}
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        fallback_text = _safe_str(output)
        if fallback_text is None:
            fallback_text = f"<{type(output).__name__}>"
        encoded = json.dumps(
            {"status": status, "output": fallback_text},
            separators=(",", ":"),
            ensure_ascii=True,
        )
    if len(encoded) <= _MAX_TOOL_RESULT_CHARS:
        return encoded

    if isinstance(output, str):
        raw_preview = output
    else:
        try:
            raw_preview = json.dumps(
                output, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        except (TypeError, ValueError, RecursionError):
            fallback_preview = _safe_str(output)
            raw_preview = (
                fallback_preview if fallback_preview is not None else f"<{type(output).__name__}>"
            )
    raw_preview = raw_preview[:_TOOL_RESULT_TRUNCATE_CHARS]

    lo, hi = 0, len(raw_preview)
    best: str | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = json.dumps(
            {"status": status, "truncated": True, "output": raw_preview[:mid]},
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(candidate) <= _MAX_TOOL_RESULT_CHARS:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    return json.dumps(
        {"status": status, "truncated": True, "output": ""},
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _output_has_secret(output: dict[str, object] | str) -> bool:
    texts: list[str] = []
    _walk_strings(output, texts)
    if not isinstance(output, str):
        try:
            texts.append(json.dumps(output, separators=(",", ":"), ensure_ascii=False))
        except (TypeError, ValueError, RecursionError):
            text_repr = _safe_str(output)
            if text_repr:
                texts.append(text_repr)
    if not texts:
        return False
    return contains_high_confidence_secret(_chunked_scan_messages(texts))


def _texts_have_secret(texts: Sequence[str]) -> bool:
    if not texts:
        return False
    expanded: list[str] = []
    for text in texts:
        _walk_strings(text, expanded)
    if not expanded:
        return False
    return contains_high_confidence_secret(_chunked_scan_messages(expanded))


class AssistantChatService:
    """Thread-safe multi-turn cloud LLM chat coordinator for MAX."""

    def __init__(
        self,
        config: DesktopConfig,
        *,
        reply_mode: ReplyMode = ReplyMode.CONVERSATION,
        max_tool_iterations: int = 5,
        max_output_tokens: int = 1024,
        max_history_messages: int = 32,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> None:
        if not isinstance(config, DesktopConfig):
            raise ValueError("config must be a DesktopConfig")
        if not isinstance(reply_mode, ReplyMode):
            raise PersonaError("invalid_reply_mode")
        if type(max_tool_iterations) is not int or not 1 <= max_tool_iterations <= 10:
            raise ValueError("max_tool_iterations must be between 1 and 10")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32_000:
            raise ValueError("max_output_tokens must be between 1 and 32,000")
        if (
            type(max_history_messages) is not int
            or not 2 <= max_history_messages <= _MAX_HISTORY_MESSAGES_CAP
        ):
            raise ValueError("max_history_messages must be between 2 and 46")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 1.0 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError("timeout_seconds must be between 1 and 120")

        self._config = config
        self._reply_mode = reply_mode
        self._max_tool_iterations = max_tool_iterations
        self._max_output_tokens = max_output_tokens
        self._max_history_messages = max_history_messages
        self._timeout_seconds = float(timeout_seconds)
        self._client = client
        self._transport = transport
        self._client_factory = client_factory
        self._base_tools = tuple(tools)
        if len(self._base_tools) > 32 or any(
            not isinstance(tool, ToolDefinition) for tool in self._base_tools
        ):
            raise ValueError("tools must contain at most 32 ToolDefinition entries")

        self._lock = threading.Lock()
        self._history: list[tuple[ChatMessage, ...]] = []
        self._pending_approvals: dict[str, _PendingApproval] = {}

    def __repr__(self) -> str:
        status = self.provider_status()
        with self._lock:
            mode = self._reply_mode.value
        return (
            f"AssistantChatService(configured={status.configured}, "
            f"provider={status.provider!r}, model_id={status.model_id!r}, reply_mode={mode!r})"
        )

    def provider_status(self) -> ProviderStatus:
        cfg = self._config.active_provider
        if cfg is None:
            return ProviderStatus(configured=False, provider=None, model_id=None)
        configured = isinstance(cfg.api_key, str) and bool(cfg.api_key.strip())
        return ProviderStatus(configured=configured, provider=cfg.provider, model_id=cfg.model_id)

    def set_reply_mode(self, mode: ReplyMode) -> None:
        if not isinstance(mode, ReplyMode):
            raise PersonaError("invalid_reply_mode")
        with self._lock:
            self._reply_mode = mode

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
            self._pending_approvals.clear()

    def history_len(self) -> int:
        with self._lock:
            return sum(len(group) for group in self._history)

    def pending_approval_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending_approvals.keys())

    def send_message(
        self,
        user_text: str,
        *,
        tool_executor: ToolExecutor | None = None,
        on_tool_activity: Callable[[str, str], None] | None = None,
    ) -> ChatTurnResult:
        """Synchronous chat entrypoint safe for ``QThreadPool`` worker threads."""
        return asyncio.run(
            self.send_message_async(
                user_text, tool_executor=tool_executor, on_tool_activity=on_tool_activity
            )
        )

    def resume_approval(
        self,
        approval_id: str,
        *,
        tool_executor: ToolExecutor,
    ) -> ChatTurnResult:
        """Synchronous approval-resume entrypoint safe for ``QThreadPool`` worker threads."""
        return asyncio.run(self.resume_approval_async(approval_id, tool_executor=tool_executor))

    def _provider_identity(self) -> tuple[str | None, str | None]:
        cfg = self._config.active_provider
        if cfg is None:
            return None, None
        return cfg.provider, cfg.model_id

    def _resolve_tools(
        self,
        tool_executor: ToolExecutor | None,
    ) -> tuple[ToolDefinition, ...]:
        """Do not advertise private local reads to a cloud provider.

        A model's call to a read-only tool is NOT consent to send its output
        (filenames, task details, notes, or file contents) off the device.
        Owner-approved export must be a separate, host-enforced capability.
        """
        private_reads = {
            "workspace.list_files",
            "workspace.read_file",
            "notes.list",
            "task.list",
            "task.status",
            "system.status",
        }
        if tool_executor is None:
            candidates = self._base_tools
        else:
            executor_tools = tuple(tool_executor.tool_definitions())
            seen = {tool.name for tool in executor_tools}
            merged = list(executor_tools)
            for tool in self._base_tools:
                if tool.name not in seen:
                    seen.add(tool.name)
                    merged.append(tool)
            candidates = tuple(merged)
        return tuple(tool for tool in candidates if tool.name not in private_reads)

    def _prepare_request(
        self,
        *,
        spec: ModelSpec,
        max_cost_decimal: Decimal,
        mode: ReplyMode,
        history_groups: list[tuple[ChatMessage, ...]],
        turn_messages: Sequence[ChatMessage],
        wire_tools: tuple[ToolDefinition, ...],
    ) -> GatewayRequest | ChatTurnResult:
        provider_id, model_id = spec.provider_id, spec.model_id
        persona_msg = persona_message(mode)
        in_rate = spec.input_usd_per_million or Decimal(0)
        out_rate = spec.output_usd_per_million or Decimal(0)
        max_cost_scaled = max_cost_decimal * Decimal(1_000_000)

        working_groups = list(history_groups)
        max_history_msgs = min(self._max_history_messages, _MAX_PRE_PERSONA_MESSAGES)

        while True:
            history_msgs = [msg for group in working_groups for msg in group]
            candidate_msgs = [*history_msgs, *turn_messages]
            if len(candidate_msgs) > _MAX_PRE_PERSONA_MESSAGES or (
                working_groups and len(candidate_msgs) > max_history_msgs
            ):
                if working_groups:
                    working_groups.pop(0)
                    continue
                return ChatTurnResult(
                    ok=False,
                    reply="Conversation turn exceeds the maximum supported message count.",
                    provider=provider_id,
                    model_id=model_id,
                    error_code="invalid_request",
                )

            try:
                envelope_bytes = request_envelope_bytes((persona_msg, *candidate_msgs), wire_tools)
            except (TypeError, ValueError, RecursionError):
                return ChatTurnResult(
                    ok=False,
                    reply="The chat request could not be constructed safely.",
                    provider=provider_id,
                    model_id=model_id,
                    error_code="invalid_request",
                )
            max_input_tokens = envelope_bytes + _INPUT_ENVELOPE_MARGIN_BYTES
            input_cost_scaled = Decimal(max_input_tokens) * in_rate

            too_large_for_context = max_input_tokens + self._max_output_tokens > spec.context_tokens
            over_default_budget = (
                input_cost_scaled + Decimal(self._max_output_tokens) * out_rate > max_cost_scaled
            )
            if working_groups and (too_large_for_context or over_default_budget):
                working_groups.pop(0)
                continue
            break

        if max_input_tokens >= spec.context_tokens:
            return ChatTurnResult(
                ok=False,
                reply="The message exceeds the model context window.",
                provider=provider_id,
                model_id=model_id,
                error_code="invalid_request",
            )

        effective_max_out = min(self._max_output_tokens, spec.context_tokens - max_input_tokens)
        remaining_scaled = max_cost_scaled - Decimal(max_input_tokens) * in_rate
        if remaining_scaled < 0:
            return ChatTurnResult(
                ok=False,
                reply="Request exceeds the configured per-turn USD spend ceiling.",
                provider=provider_id,
                model_id=model_id,
                error_code="budget_exceeded",
            )
        if out_rate > 0:
            affordable_out = int(remaining_scaled // out_rate)
            min_floor = min(_MIN_OUTPUT_TOKEN_FLOOR, self._max_output_tokens)
            if affordable_out < min_floor:
                return ChatTurnResult(
                    ok=False,
                    reply="Request exceeds the configured per-turn USD spend ceiling.",
                    provider=provider_id,
                    model_id=model_id,
                    error_code="budget_exceeded",
                )
            effective_max_out = min(effective_max_out, affordable_out)

        if effective_max_out < 1:
            return ChatTurnResult(
                ok=False,
                reply="Request exceeds the configured per-turn USD spend ceiling.",
                provider=provider_id,
                model_id=model_id,
                error_code="budget_exceeded",
            )

        capabilities = frozenset({"text", "tools"}) if wire_tools else frozenset({"text"})
        try:
            base_request = GatewayRequest(
                request_id=uuid4().hex,
                messages=tuple(candidate_msgs),
                privacy=Privacy.INTERNAL,
                max_input_tokens=max_input_tokens,
                max_output_tokens=effective_max_out,
                max_cost_usd=max_cost_decimal,
                cloud_allowed=True,
                local_only=False,
                required_capabilities=capabilities,
                allowed_providers=frozenset({spec.provider_id}),
                allow_fallback=False,
                timeout_seconds=self._timeout_seconds,
                tools=wire_tools,
            )
            return with_persona(base_request, mode=mode)
        except (PersonaError, ValueError, RecursionError):
            return ChatTurnResult(
                ok=False,
                reply="The chat request could not be constructed safely.",
                provider=provider_id,
                model_id=model_id,
                error_code="invalid_request",
            )

    def _map_gateway_error(
        self,
        exc: Exception,
        *,
        provider: str,
        model_id: str,
        summaries: Sequence[ToolExecutionSummary],
        total_cost: Decimal | None,
    ) -> ChatTurnResult:
        if isinstance(exc, GatewayError):
            error_code, message = _GATEWAY_ERROR_MESSAGES.get(
                exc.code,
                ("provider_error", "The AI provider returned an unexpected error."),
            )
        elif isinstance(exc, httpx.TimeoutException):
            error_code, message = _GATEWAY_ERROR_MESSAGES["timeout"]
        elif isinstance(exc, httpx.RequestError):
            error_code, message = (
                "network_error",
                "A network error occurred while contacting the AI provider.",
            )
        else:
            error_code, message = (
                "provider_error",
                "An unexpected error occurred while contacting the AI provider.",
            )
        return ChatTurnResult(
            ok=False,
            reply=message,
            provider=provider,
            model_id=model_id,
            error_code=error_code,
            tool_executions=tuple(summaries),
            estimated_cost_usd=float(total_cost) if total_cost is not None else None,
        )

    async def _complete_once(
        self,
        spec: ModelSpec,
        request: GatewayRequest,
    ) -> GatewayResponse:
        if self._client is not None:
            return await Gateway((spec,), HTTPAdapter(self._client)).complete(request)
        if self._client_factory is not None:
            async with self._client_factory() as client:
                return await Gateway((spec,), HTTPAdapter(client)).complete(request)
        async with httpx.AsyncClient(trust_env=False, transport=self._transport) as client:
            return await Gateway((spec,), HTTPAdapter(client)).complete(request)

    def _commit_group(
        self,
        group: tuple[ChatMessage, ...],
        *,
        replace_group: tuple[ChatMessage, ...] | None = None,
    ) -> None:
        with self._lock:
            if replace_group is not None:
                for idx, existing in enumerate(self._history):
                    if existing is replace_group:
                        self._history[idx] = group
                        break
                else:
                    self._history.append(group)
            else:
                self._history.append(group)
            while (
                len(self._history) > 1
                and sum(len(item) for item in self._history) > self._max_history_messages
            ):
                self._history.pop(0)

    def _store_pending_and_commit_placeholder(
        self,
        *,
        approval_id: str,
        wire_proposal: ToolProposal,
        canonical_proposal: ToolProposal,
        remaining_proposals: tuple[ToolProposal, ...],
        turn_messages: tuple[ChatMessage, ...],
        prior_summaries: tuple[ToolExecutionSummary, ...],
        user_msg: ChatMessage,
        pending_reply: str,
        codec: _ToolNameCodec,
        spent_cost: Decimal | None,
        replace_group: tuple[ChatMessage, ...] | None = None,
    ) -> None:
        safe_reply = pending_reply.encode("utf-8", errors="replace").decode("utf-8")
        placeholder = (user_msg, ChatMessage("assistant", _bound_message_text(safe_reply)))
        pending = _PendingApproval(
            approval_id=approval_id,
            wire_proposal=wire_proposal,
            canonical_proposal=canonical_proposal,
            remaining_proposals=remaining_proposals,
            turn_messages=turn_messages,
            prior_summaries=prior_summaries,
            placeholder_group=placeholder,
            codec=codec,
            spent_cost=spent_cost,
        )
        with self._lock:
            self._pending_approvals[approval_id] = pending
            if replace_group is not None:
                for idx, existing in enumerate(self._history):
                    if existing is replace_group:
                        self._history[idx] = placeholder
                        break
                else:
                    self._history.append(placeholder)
            else:
                self._history.append(placeholder)
            while (
                len(self._history) > 1
                and sum(len(item) for item in self._history) > self._max_history_messages
            ):
                self._history.pop(0)

    async def send_message_async(
        self,
        user_text: str,
        *,
        tool_executor: ToolExecutor | None = None,
        on_tool_activity: Callable[[str, str], None] | None = None,
    ) -> ChatTurnResult:
        """Async chat turn with persona injection, egress guard, and bounded tool loop."""
        p_name, m_id = self._provider_identity()
        if not isinstance(user_text, str) or not user_text.strip():
            return ChatTurnResult(
                ok=False,
                reply="Please enter a non-empty message.",
                provider=p_name,
                model_id=m_id,
                error_code="invalid_input",
            )
        if len(user_text) > _MAX_USER_INPUT_CHARS:
            return ChatTurnResult(
                ok=False,
                reply="Message exceeds the 32,000-character limit.",
                provider=p_name,
                model_id=m_id,
                error_code="invalid_input",
            )
        try:
            user_text.encode("utf-8")
        except UnicodeEncodeError:
            return ChatTurnResult(
                ok=False,
                reply="Message contains invalid Unicode characters.",
                provider=p_name,
                model_id=m_id,
                error_code="invalid_input",
            )

        if _texts_have_secret((user_text,)):
            return ChatTurnResult(
                ok=False,
                reply="Message blocked because it appears to contain sensitive credentials or secrets.",
                provider=p_name,
                model_id=m_id,
                error_code="egress_blocked",
            )

        provider_cfg = self._config.active_provider
        if (
            provider_cfg is None
            or not isinstance(provider_cfg.api_key, str)
            or not provider_cfg.api_key.strip()
        ):
            return ChatTurnResult(
                ok=False,
                reply=(
                    "No cloud API key is configured. Add OPENAI_API_KEY or "
                    "ANTHROPIC_API_KEY to your .env or local config."
                ),
                provider=p_name,
                model_id=m_id,
                error_code="missing_api_key",
            )

        try:
            raw_tools = self._resolve_tools(tool_executor)
            codec = _ToolNameCodec.build(raw_tools)
            spec, max_cost_decimal = _build_model_spec(
                provider_cfg, has_tools=bool(codec.wire_tools)
            )
        except (ValueError, RecursionError):
            return ChatTurnResult(
                ok=False,
                reply="Provider or tool configuration is invalid.",
                provider=provider_cfg.provider,
                model_id=provider_cfg.model_id,
                error_code="invalid_provider_config",
            )

        with self._lock:
            history_snapshot = list(self._history)
            mode = self._reply_mode

        user_msg = ChatMessage("user", user_text)
        return await self._run_turn_loop(
            spec=spec,
            max_cost_decimal=max_cost_decimal,
            mode=mode,
            history_snapshot=history_snapshot,
            turn_messages=[user_msg],
            user_msg=user_msg,
            codec=codec,
            tool_executor=tool_executor,
            summaries=[],
            total_cost=None,
            replace_group=None,
            on_tool_activity=on_tool_activity,
        )

    async def resume_approval_async(
        self,
        approval_id: str,
        *,
        tool_executor: ToolExecutor,
    ) -> ChatTurnResult:
        """Resume a paused tool-calling turn after an owner Trust Inbox decision."""
        p_name, m_id = self._provider_identity()
        if not isinstance(approval_id, str) or not approval_id.strip():
            return ChatTurnResult(
                ok=False,
                reply="No pending approval found for that identifier.",
                provider=p_name,
                model_id=m_id,
                error_code="approval_not_found",
            )

        with self._lock:
            pending = self._pending_approvals.pop(approval_id, None)
            history_snapshot = list(self._history)
            mode = self._reply_mode

        if pending is None:
            return ChatTurnResult(
                ok=False,
                reply="No pending approval found for that identifier.",
                provider=p_name,
                model_id=m_id,
                error_code="approval_not_found",
            )

        summaries = list(pending.prior_summaries)
        turn_messages = list(pending.turn_messages)
        user_msg = turn_messages[0]
        total_cost = pending.spent_cost
        codec = pending.codec

        summary = self._execute_proposal(
            tool_executor,
            pending.canonical_proposal,
            approval_id=approval_id,
        )
        summaries.append(summary)

        if summary.status == "awaiting_approval":
            next_approval_id = summary.approval_id
            if not next_approval_id:
                summary = ToolExecutionSummary(
                    call_id=summary.call_id,
                    tool_name=summary.tool_name,
                    arguments=summary.arguments,
                    status="failed",
                    output="missing_approval_identifier",
                    task_id=summary.task_id,
                )
                summaries[-1] = summary
            else:
                pending_reply = (
                    f"Approval ({next_approval_id}) is still required in the Trust Inbox "
                    f"before '{summary.tool_name}' can run."
                )
                self._store_pending_and_commit_placeholder(
                    approval_id=next_approval_id,
                    wire_proposal=pending.wire_proposal,
                    canonical_proposal=pending.canonical_proposal,
                    remaining_proposals=pending.remaining_proposals,
                    turn_messages=tuple(turn_messages),
                    prior_summaries=tuple(summaries),
                    user_msg=user_msg,
                    pending_reply=pending_reply,
                    codec=codec,
                    spent_cost=total_cost,
                    replace_group=pending.placeholder_group,
                )
                return ChatTurnResult(
                    ok=True,
                    reply=pending_reply,
                    provider=p_name,
                    model_id=m_id,
                    error_code=None,
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )

        if summary.status == "denied":
            denial_reply = (
                f"Action '{pending.canonical_proposal.name}' was denied and was not executed."
            )
            self._commit_group(
                (user_msg, ChatMessage("assistant", denial_reply)),
                replace_group=pending.placeholder_group,
            )
            return ChatTurnResult(
                ok=True,
                reply=denial_reply,
                provider=p_name,
                model_id=m_id,
                error_code=None,
                tool_executions=tuple(summaries),
                estimated_cost_usd=float(total_cost) if total_cost is not None else None,
            )

        if _output_has_secret(summary.output):
            return ChatTurnResult(
                ok=False,
                reply="Tool output blocked because it appears to contain sensitive credentials or secrets.",
                provider=p_name,
                model_id=m_id,
                error_code="egress_blocked",
                tool_executions=tuple(summaries),
                estimated_cost_usd=float(total_cost) if total_cost is not None else None,
            )

        turn_messages.append(
            ChatMessage(
                role="tool",
                content=_serialize_tool_output(summary.status, summary.output),
                tool_call_id=pending.wire_proposal.call_id,
                is_error=(summary.status == "failed"),
            )
        )

        # Execute any remaining sibling proposals from the same assistant batch.
        for idx, wire_call in enumerate(pending.remaining_proposals):
            if wire_call.name not in codec.wire_to_canonical:
                undeclared_summary = ToolExecutionSummary(
                    call_id=wire_call.call_id,
                    tool_name=wire_call.name,
                    arguments=dict[str, object](wire_call.arguments),
                    status="denied",
                    output="undeclared_tool",
                )
                summaries.append(undeclared_summary)
                turn_messages.append(
                    ChatMessage(
                        role="tool",
                        content=_serialize_tool_output(
                            undeclared_summary.status, undeclared_summary.output
                        ),
                        tool_call_id=wire_call.call_id,
                        is_error=True,
                    )
                )
                continue
            canonical_name = codec.to_canonical(wire_call.name)
            canonical_call = ToolProposal(
                call_id=wire_call.call_id,
                name=canonical_name,
                arguments=dict(wire_call.arguments),
            )
            next_summary = self._execute_proposal(tool_executor, canonical_call)
            summaries.append(next_summary)
            if next_summary.status == "awaiting_approval":
                next_approval_id = next_summary.approval_id
                if not next_approval_id:
                    next_summary = ToolExecutionSummary(
                        call_id=next_summary.call_id,
                        tool_name=next_summary.tool_name,
                        arguments=next_summary.arguments,
                        status="failed",
                        output="missing_approval_identifier",
                        task_id=next_summary.task_id,
                    )
                    summaries[-1] = next_summary
                else:
                    pending_reply = (
                        f"Approval required in Trust Inbox for '{next_summary.tool_name}' "
                        f"(approval {next_approval_id})."
                    )
                    self._store_pending_and_commit_placeholder(
                        approval_id=next_approval_id,
                        wire_proposal=wire_call,
                        canonical_proposal=canonical_call,
                        remaining_proposals=pending.remaining_proposals[idx + 1 :],
                        turn_messages=tuple(turn_messages),
                        prior_summaries=tuple(summaries),
                        user_msg=user_msg,
                        pending_reply=pending_reply,
                        codec=codec,
                        spent_cost=total_cost,
                        replace_group=pending.placeholder_group,
                    )
                    return ChatTurnResult(
                        ok=True,
                        reply=pending_reply,
                        provider=p_name,
                        model_id=m_id,
                        error_code=None,
                        tool_executions=tuple(summaries),
                        estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                    )
            if _output_has_secret(next_summary.output):
                return ChatTurnResult(
                    ok=False,
                    reply=(
                        "Tool output blocked because it appears to contain sensitive "
                        "credentials or secrets."
                    ),
                    provider=p_name,
                    model_id=m_id,
                    error_code="egress_blocked",
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )
            turn_messages.append(
                ChatMessage(
                    role="tool",
                    content=_serialize_tool_output(next_summary.status, next_summary.output),
                    tool_call_id=wire_call.call_id,
                    is_error=(next_summary.status in {"denied", "failed"}),
                )
            )

        provider_cfg = self._config.active_provider
        if (
            provider_cfg is None
            or not isinstance(provider_cfg.api_key, str)
            or not provider_cfg.api_key.strip()
        ):
            return ChatTurnResult(
                ok=False,
                reply=(
                    "No cloud API key is configured. Add OPENAI_API_KEY or "
                    "ANTHROPIC_API_KEY to your .env or local config."
                ),
                provider=p_name,
                model_id=m_id,
                error_code="missing_api_key",
                tool_executions=tuple(summaries),
                estimated_cost_usd=float(total_cost) if total_cost is not None else None,
            )

        try:
            spec, max_cost_decimal = _build_model_spec(
                provider_cfg, has_tools=bool(codec.wire_tools)
            )
        except (ValueError, RecursionError):
            return ChatTurnResult(
                ok=False,
                reply="Provider configuration is invalid.",
                provider=provider_cfg.provider,
                model_id=provider_cfg.model_id,
                error_code="invalid_provider_config",
                tool_executions=tuple(summaries),
                estimated_cost_usd=float(total_cost) if total_cost is not None else None,
            )

        # Remove placeholder group from history snapshot when building the continuation request
        # so the same user message is not duplicated.
        filtered_history = [g for g in history_snapshot if g is not pending.placeholder_group]
        return await self._run_turn_loop(
            spec=spec,
            max_cost_decimal=max_cost_decimal,
            mode=mode,
            history_snapshot=filtered_history,
            turn_messages=turn_messages,
            user_msg=user_msg,
            codec=codec,
            tool_executor=tool_executor,
            summaries=summaries,
            total_cost=total_cost,
            replace_group=pending.placeholder_group,
        )

    @staticmethod
    def _execute_proposal(
        tool_executor: ToolExecutor,
        proposal: ToolProposal,
        *,
        approval_id: str | None = None,
    ) -> ToolExecutionSummary:
        try:
            return tool_executor.execute(proposal, approval_id=approval_id)
        except Exception:
            return ToolExecutionSummary(
                call_id=proposal.call_id,
                tool_name=proposal.name,
                arguments=dict[str, object](proposal.arguments),
                status="failed",
                output="tool_execution_error",
            )

    async def _run_turn_loop(
        self,
        *,
        spec: ModelSpec,
        max_cost_decimal: Decimal,
        mode: ReplyMode,
        history_snapshot: list[tuple[ChatMessage, ...]],
        turn_messages: list[ChatMessage],
        user_msg: ChatMessage,
        codec: _ToolNameCodec,
        tool_executor: ToolExecutor | None,
        summaries: list[ToolExecutionSummary],
        total_cost: Decimal | None,
        replace_group: tuple[ChatMessage, ...] | None,
        on_tool_activity: Callable[[str, str], None] | None = None,
    ) -> ChatTurnResult:
        failures = ToolFailureGuard()

        def emit(name: str, status: str) -> None:
            if on_tool_activity is not None:
                try:
                    on_tool_activity(name, status)
                except Exception:
                    pass  # status UI never controls tool execution or approvals

        for _iteration in range(self._max_tool_iterations):
            spent_cost = total_cost if total_cost is not None else Decimal(0)
            remaining_budget = max_cost_decimal - spent_cost
            if total_cost is not None and remaining_budget <= Decimal(0):
                return ChatTurnResult(
                    ok=False,
                    reply="Request exceeds the configured per-turn USD spend ceiling.",
                    provider=spec.provider_id,
                    model_id=spec.model_id,
                    error_code="budget_exceeded",
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost),
                )

            prepared = self._prepare_request(
                spec=spec,
                max_cost_decimal=remaining_budget,
                mode=mode,
                history_groups=history_snapshot,
                turn_messages=turn_messages,
                wire_tools=codec.wire_tools,
            )
            if isinstance(prepared, ChatTurnResult):
                return ChatTurnResult(
                    ok=prepared.ok,
                    reply=prepared.reply,
                    provider=prepared.provider,
                    model_id=prepared.model_id,
                    error_code=prepared.error_code,
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )

            if request_has_high_confidence_secret(prepared.messages, prepared.tools):
                return ChatTurnResult(
                    ok=False,
                    reply=(
                        "Message blocked because it appears to contain sensitive "
                        "credentials or secrets."
                    ),
                    provider=spec.provider_id,
                    model_id=spec.model_id,
                    error_code="egress_blocked",
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )

            try:
                response = await self._complete_once(spec, prepared)
            except Exception as exc:
                return self._map_gateway_error(
                    exc,
                    provider=spec.provider_id,
                    model_id=spec.model_id,
                    summaries=summaries,
                    total_cost=total_cost,
                )

            if response.estimated_cost_usd is not None:
                total_cost = (total_cost or Decimal(0)) + response.estimated_cost_usd

            if not response.tool_proposals:
                try:
                    assistant_msg = ChatMessage("assistant", _bound_message_text(response.content))
                except (ValueError, RecursionError):
                    return ChatTurnResult(
                        ok=False,
                        reply="The AI provider returned a malformed response.",
                        provider=spec.provider_id,
                        model_id=spec.model_id,
                        error_code="provider_error",
                        tool_executions=tuple(summaries),
                        estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                    )
                turn_messages.append(assistant_msg)
                self._commit_group(tuple(turn_messages), replace_group=replace_group)
                return ChatTurnResult(
                    ok=True,
                    reply=response.content,
                    provider=spec.provider_id,
                    model_id=response.model_id,
                    error_code=None,
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )

            if tool_executor is None:
                for wire_call in response.tool_proposals:
                    if wire_call.name in codec.wire_to_canonical:
                        canonical_name = codec.to_canonical(wire_call.name)
                        reason = "tool_execution_unavailable"
                    else:
                        canonical_name = wire_call.name
                        reason = "undeclared_tool"
                    summaries.append(
                        ToolExecutionSummary(
                            call_id=wire_call.call_id,
                            tool_name=canonical_name,
                            arguments=dict[str, object](wire_call.arguments),
                            status="denied",
                            output=reason,
                        )
                    )
                reply_text = response.content or "Tool execution is not enabled for this session."
                try:
                    assistant_reply_msg = ChatMessage("assistant", _bound_message_text(reply_text))
                except (ValueError, RecursionError):
                    return ChatTurnResult(
                        ok=False,
                        reply="The AI provider returned a malformed response.",
                        provider=spec.provider_id,
                        model_id=spec.model_id,
                        error_code="provider_error",
                        tool_executions=tuple(summaries),
                        estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                    )
                self._commit_group(
                    (user_msg, assistant_reply_msg),
                    replace_group=replace_group,
                )
                return ChatTurnResult(
                    ok=True,
                    reply=reply_text,
                    provider=spec.provider_id,
                    model_id=response.model_id,
                    error_code=None,
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )

            try:
                assistant_tool_msg = ChatMessage(
                    role="assistant",
                    content=_bound_message_text(response.content),
                    tool_calls=response.tool_proposals,
                )
            except (ValueError, RecursionError):
                return ChatTurnResult(
                    ok=False,
                    reply="The AI provider returned an invalid tool proposal.",
                    provider=spec.provider_id,
                    model_id=spec.model_id,
                    error_code="provider_error",
                    tool_executions=tuple(summaries),
                    estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                )
            turn_messages.append(assistant_tool_msg)
            for idx, wire_call in enumerate(response.tool_proposals):
                if wire_call.name not in codec.wire_to_canonical:
                    summary = ToolExecutionSummary(
                        call_id=wire_call.call_id,
                        tool_name=wire_call.name,
                        arguments=dict[str, object](wire_call.arguments),
                        status="denied",
                        output="undeclared_tool",
                    )
                    summaries.append(summary)
                    turn_messages.append(
                        ChatMessage(
                            role="tool",
                            content=_serialize_tool_output(summary.status, summary.output),
                            tool_call_id=wire_call.call_id,
                            is_error=True,
                        )
                    )
                    continue

                canonical_name = codec.to_canonical(wire_call.name)
                canonical_call = ToolProposal(
                    call_id=wire_call.call_id,
                    name=canonical_name,
                    arguments=dict(wire_call.arguments),
                )
                if failures.blocked(canonical_name, canonical_call.arguments):
                    return ChatTurnResult(
                        ok=False,
                        reply=(
                            "I stopped repeating a failing or denied tool call. "
                            "Review the recorded error and choose a different approach."
                        ),
                        provider=spec.provider_id,
                        model_id=response.model_id,
                        error_code="repeated_tool_failure",
                        tool_executions=tuple(summaries),
                        estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                    )
                emit(canonical_name, "proposed")
                summary = self._execute_proposal(tool_executor, canonical_call)
                emit(canonical_name, summary.status)
                summaries.append(summary)

                if summary.status == "awaiting_approval":
                    approval_id = summary.approval_id
                    if not approval_id:
                        summary = ToolExecutionSummary(
                            call_id=summary.call_id,
                            tool_name=summary.tool_name,
                            arguments=summary.arguments,
                            status="failed",
                            output="missing_approval_identifier",
                            task_id=summary.task_id,
                        )
                        summaries[-1] = summary
                    else:
                        pending_reply = (
                            f"Approval required in Trust Inbox for '{summary.tool_name}' "
                            f"(approval {approval_id})."
                        )
                        self._store_pending_and_commit_placeholder(
                            approval_id=approval_id,
                            wire_proposal=wire_call,
                            canonical_proposal=canonical_call,
                            remaining_proposals=response.tool_proposals[idx + 1 :],
                            turn_messages=tuple(turn_messages),
                            prior_summaries=tuple(summaries),
                            user_msg=user_msg,
                            pending_reply=pending_reply,
                            codec=codec,
                            spent_cost=total_cost,
                            replace_group=replace_group,
                        )
                        return ChatTurnResult(
                            ok=True,
                            reply=pending_reply,
                            provider=spec.provider_id,
                            model_id=response.model_id,
                            error_code=None,
                            tool_executions=tuple(summaries),
                            estimated_cost_usd=(
                                float(total_cost) if total_cost is not None else None
                            ),
                        )

                if _output_has_secret(summary.output):
                    return ChatTurnResult(
                        ok=False,
                        reply=(
                            "Tool output blocked because it appears to contain sensitive "
                            "credentials or secrets."
                        ),
                        provider=spec.provider_id,
                        model_id=spec.model_id,
                        error_code="egress_blocked",
                        tool_executions=tuple(summaries),
                        estimated_cost_usd=float(total_cost) if total_cost is not None else None,
                    )

                feedback = _serialize_tool_output(summary.status, summary.output)
                if summary.status in {"denied", "failed"}:
                    hint = failures.failed(
                        canonical_name,
                        canonical_call.arguments,
                        summary.output,
                        denied=summary.status == "denied",
                    )
                    if summary.status == "failed":
                        feedback = json.dumps(
                            {"result": json.loads(feedback), "recovery_hint": hint}
                        )
                elif summary.status == "completed":
                    failures.success(canonical_name, canonical_call.arguments)
                turn_messages.append(
                    ChatMessage(
                        role="tool",
                        content=_bound_message_text(feedback),
                        tool_call_id=wire_call.call_id,
                        is_error=(summary.status in {"denied", "failed"}),
                    )
                )

        limit_reply = "I stopped after reaching the maximum number of tool steps for this turn."
        self._commit_group(
            (user_msg, ChatMessage("assistant", limit_reply)),
            replace_group=replace_group,
        )
        return ChatTurnResult(
            ok=True,
            reply=limit_reply,
            provider=spec.provider_id,
            model_id=spec.model_id,
            error_code=None,
            tool_executions=tuple(summaries),
            estimated_cost_usd=float(total_cost) if total_cost is not None else None,
        )
