"""Strict normalization of text, token usage and inert tool proposals."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from max_core.gateway.contracts import (
    GatewayError,
    GatewayResponse,
    ModelSpec,
    TokenUsage,
    ToolProposal,
)

MAX_TEXT = 128_000
MAX_ARGS = 32_000
MAX_TOOLS = 16


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError("malformed_provider_response")
    return value


def _int_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GatewayError("malformed_provider_response")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise GatewayError("malformed_provider_response")
    return value


def _tool(call_id: Any, name: Any, arguments: Any) -> ToolProposal:
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
        raise GatewayError("invalid_tool_proposal")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}", name):
        raise GatewayError("invalid_tool_proposal")
    if isinstance(arguments, str):
        if len(arguments) > MAX_ARGS:
            raise GatewayError("invalid_tool_proposal")
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError) as exc:
            raise GatewayError("invalid_tool_proposal") from exc
    if not isinstance(arguments, dict) or len(json.dumps(arguments)) > MAX_ARGS:
        raise GatewayError("invalid_tool_proposal")
    return ToolProposal(call_id=call_id, name=name, arguments=arguments)


def _usage(payload: Any, input_key: str, output_key: str) -> TokenUsage | None:
    if payload is None:
        return None
    values = _mapping(payload)
    input_count = _int_count(values.get(input_key))
    output_count = _int_count(values.get(output_key))
    if input_count is None and output_count is None:
        return None
    return TokenUsage(input_tokens=input_count, output_tokens=output_count)


def actual_cost(spec: ModelSpec, usage: TokenUsage | None) -> Decimal | None:
    if (usage is None or usage.input_tokens is None or usage.output_tokens is None or
            spec.input_usd_per_million is None or spec.output_usd_per_million is None):
        return None
    return (
        Decimal(usage.input_tokens) * spec.input_usd_per_million
        + Decimal(usage.output_tokens) * spec.output_usd_per_million
    ) / Decimal(1_000_000)


def normalize_openai(data: Any, request_id: str, spec: ModelSpec) -> GatewayResponse:
    obj = _mapping(data)
    choices = obj.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise GatewayError("malformed_provider_response")
    choice = _mapping(choices[0])
    message = _mapping(choice.get("message"))
    if message.get("role") != "assistant":
        raise GatewayError("malformed_provider_response")
    content = message.get("content")
    if content is None:
        content = ""
    content = _text(content)
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > MAX_TOOLS:
        raise GatewayError("invalid_tool_proposal")
    tools: list[ToolProposal] = []
    for raw in calls:
        call = _mapping(raw)
        function = _mapping(call.get("function"))
        tools.append(_tool(call.get("id"), function.get("name"), function.get("arguments")))
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise GatewayError("malformed_provider_response")
    usage = _usage(obj.get("usage"), "prompt_tokens", "completion_tokens")
    return GatewayResponse(
        request_id, spec.provider_id, spec.model_id, content, tuple(tools),
        finish_reason, usage, actual_cost(spec, usage),
    )


def normalize_anthropic(data: Any, request_id: str, spec: ModelSpec) -> GatewayResponse:
    obj = _mapping(data)
    if obj.get("type") != "message" or obj.get("role") != "assistant":
        raise GatewayError("malformed_provider_response")
    blocks = obj.get("content")
    if not isinstance(blocks, list) or len(blocks) > 100:
        raise GatewayError("malformed_provider_response")
    texts: list[str] = []
    tools: list[ToolProposal] = []
    for value in blocks:
        block = _mapping(value)
        if block.get("type") == "text":
            texts.append(_text(block.get("text")))
        elif block.get("type") == "tool_use":
            tools.append(_tool(block.get("id"), block.get("name"), block.get("input")))
            if len(tools) > MAX_TOOLS:
                raise GatewayError("invalid_tool_proposal")
        else:
            raise GatewayError("unsupported_capability")
    content = "".join(texts)
    if len(content) > MAX_TEXT:
        raise GatewayError("malformed_provider_response")
    stop_reason = obj.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        raise GatewayError("malformed_provider_response")
    usage = _usage(obj.get("usage"), "input_tokens", "output_tokens")
    return GatewayResponse(
        request_id, spec.provider_id, spec.model_id, content, tuple(tools),
        stop_reason, usage, actual_cost(spec, usage),
    )
