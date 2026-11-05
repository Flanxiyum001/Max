"""Bounded OpenAI / Anthropic event-stream normalization.

No model-originated tool proposal is executable; incomplete JSON is never emitted.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from max_core.gateway.contracts import (
    GatewayError,
    GatewayRequest,
    ModelSpec,
    StreamEvent,
    TokenUsage,
)
from max_core.gateway.normalize import MAX_TEXT, MAX_TOOLS, _mapping, _text, _tool, _usage

MAX_EVENTS = 5_000
MAX_STREAM_BYTES = 1_000_000


async def sse_frames(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """Parse SSE frames, bounding stream length even if the provider never finishes."""
    event = "message"
    data: list[str] = []
    total_bytes = 0
    count = 0
    async for line in lines:
        total_bytes += len(line.encode("utf-8"))
        if total_bytes > MAX_STREAM_BYTES:
            raise GatewayError("stream_too_large")
        if line == "":
            if data:
                count += 1
                if count > MAX_EVENTS:
                    raise GatewayError("stream_too_large")
                yield event, "\n".join(data)
            event = "message"
            data = []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        raise GatewayError("stream_interrupted")


def _json(data: str) -> dict[str, Any]:
    try:
        result = json.loads(data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GatewayError("malformed_provider_response") from exc
    return _mapping(result)


async def openai_events(
    frames: AsyncIterator[tuple[str, str]], request: GatewayRequest, spec: ModelSpec
) -> AsyncIterator[StreamEvent]:
    seq = 0
    calls: dict[int, dict[str, str]] = {}
    finish: str | None = None
    text_length = 0
    final_usage: TokenUsage | None = None
    finished = False
    yield StreamEvent(seq, request.request_id, spec.provider_id, spec.model_id, "start")
    seq += 1
    async for event, raw in frames:
        if raw == "[DONE]":
            finished = True
            break
        if event not in {"message", "chunk"}:
            raise GatewayError("malformed_provider_response")
        obj = _json(raw)
        if obj.get("model") is not None and obj["model"] != spec.model_id:
            raise GatewayError("model_mismatch")
        usage = _usage(obj.get("usage"), "prompt_tokens", "completion_tokens")
        if usage is not None:
            final_usage = usage
        choices = obj.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise GatewayError("malformed_provider_response")
        if not choices:
            continue  # usage-only frame
        choice = _mapping(choices[0])
        delta = _mapping(choice.get("delta"))
        content = delta.get("content")
        if content is not None:
            chunk = _text(content)
            text_length += len(chunk)
            if text_length > MAX_TEXT:
                raise GatewayError("stream_too_large")
            if chunk:
                yield StreamEvent(
                    seq,
                    request.request_id,
                    spec.provider_id,
                    spec.model_id,
                    "content_delta",
                    text=chunk,
                )
                seq += 1
        tool_deltas = delta.get("tool_calls", [])
        if not isinstance(tool_deltas, list):
            raise GatewayError("invalid_tool_proposal")
        for value in tool_deltas:
            tool = _mapping(value)
            idx = tool.get("index")
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < MAX_TOOLS:
                raise GatewayError("invalid_tool_proposal")
            if idx not in calls:
                calls[idx] = {"id": "", "name": "", "arguments": ""}
            state = calls[idx]
            call_id = tool.get("id")
            if call_id is not None:
                if not isinstance(call_id, str) or not call_id:
                    raise GatewayError("invalid_tool_proposal")
                state["id"] += call_id
            fn = tool.get("function")
            if fn is not None:
                fn = _mapping(fn)
                for key in ("name", "arguments"):
                    part = fn.get(key)
                    if part is not None:
                        if not isinstance(part, str):
                            raise GatewayError("invalid_tool_proposal")
                        state[key] += part
            if len(state["arguments"]) > 32_000:
                raise GatewayError("invalid_tool_proposal")
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise GatewayError("malformed_provider_response")
            finish = reason
    if not finished:
        raise GatewayError("stream_interrupted")
    for idx in sorted(calls):
        call = calls[idx]
        proposal = _tool(call["id"], call["name"], call["arguments"])
        yield StreamEvent(
            seq,
            request.request_id,
            spec.provider_id,
            spec.model_id,
            "tool_proposal",
            tool_proposal=proposal,
        )
        seq += 1
    if final_usage is not None:
        yield StreamEvent(
            seq, request.request_id, spec.provider_id, spec.model_id, "usage", usage=final_usage
        )
        seq += 1
    yield StreamEvent(
        seq, request.request_id, spec.provider_id, spec.model_id, "complete", finish_reason=finish
    )


async def anthropic_events(
    frames: AsyncIterator[tuple[str, str]], request: GatewayRequest, spec: ModelSpec
) -> AsyncIterator[StreamEvent]:
    seq = 0
    blocks: dict[int, dict[str, str]] = {}
    input_count: int | None = None
    output_count: int | None = None
    finish: str | None = None
    text_length = 0
    stopped = False
    tool_count = 0
    yield StreamEvent(seq, request.request_id, spec.provider_id, spec.model_id, "start")
    seq += 1
    async for kind, raw in frames:
        obj = _json(raw)
        if kind == "ping":
            continue
        if obj.get("type") != kind:
            raise GatewayError("malformed_provider_response")
        if kind == "error":
            raise GatewayError("provider_unavailable")
        if kind == "message_start":
            msg = _mapping(obj.get("message"))
            if msg.get("model") is not None and msg["model"] != spec.model_id:
                raise GatewayError("model_mismatch")
            usage = _usage(msg.get("usage"), "input_tokens", "output_tokens")
            if usage is not None:
                input_count, output_count = usage.input_tokens, usage.output_tokens
        elif kind == "content_block_start":
            idx = obj.get("index")
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < 100:
                raise GatewayError("malformed_provider_response")
            block = _mapping(obj.get("content_block"))
            typ = block.get("type")
            if typ == "text":
                initial = _text(block.get("text", ""))
                blocks[idx] = {"type": "text"}
                if initial:
                    text_length += len(initial)
                    if text_length > MAX_TEXT:
                        raise GatewayError("stream_too_large")
                    yield StreamEvent(
                        seq,
                        request.request_id,
                        spec.provider_id,
                        spec.model_id,
                        "content_delta",
                        text=initial,
                    )
                    seq += 1
            elif typ == "tool_use":
                tool_count += 1
                if tool_count > MAX_TOOLS:
                    raise GatewayError("invalid_tool_proposal")
                blocks[idx] = {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": "",
                }
            else:
                raise GatewayError("unsupported_capability")
        elif kind == "content_block_delta":
            idx = obj.get("index")
            if idx not in blocks:
                raise GatewayError("malformed_provider_response")
            block = blocks[idx]
            delta = _mapping(obj.get("delta"))
            if block["type"] == "text" and delta.get("type") == "text_delta":
                chunk = _text(delta.get("text"))
                text_length += len(chunk)
                if text_length > MAX_TEXT:
                    raise GatewayError("stream_too_large")
                if chunk:
                    yield StreamEvent(
                        seq,
                        request.request_id,
                        spec.provider_id,
                        spec.model_id,
                        "content_delta",
                        text=chunk,
                    )
                    seq += 1
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                part = delta.get("partial_json")
                if not isinstance(part, str):
                    raise GatewayError("invalid_tool_proposal")
                block["input"] += part
                if len(block["input"]) > 32_000:
                    raise GatewayError("invalid_tool_proposal")
            else:
                raise GatewayError("unsupported_capability")
        elif kind == "content_block_stop":
            idx = obj.get("index")
            if idx not in blocks:
                raise GatewayError("malformed_provider_response")
            block = blocks.pop(idx)
            if block["type"] == "tool_use":
                input_json = block["input"] or "{}"
                proposal = _tool(block["id"], block["name"], input_json)
                yield StreamEvent(
                    seq,
                    request.request_id,
                    spec.provider_id,
                    spec.model_id,
                    "tool_proposal",
                    tool_proposal=proposal,
                )
                seq += 1
        elif kind == "message_delta":
            delta = _mapping(obj.get("delta"))
            reason = delta.get("stop_reason")
            if reason is not None:
                if not isinstance(reason, str):
                    raise GatewayError("malformed_provider_response")
                finish = reason
            usage = _usage(obj.get("usage"), "input_tokens", "output_tokens")
            if usage is not None:
                input_count = usage.input_tokens if usage.input_tokens is not None else input_count
                output_count = (
                    usage.output_tokens if usage.output_tokens is not None else output_count
                )
        elif kind == "message_stop":
            stopped = True
            break
        else:
            raise GatewayError("unsupported_capability")
    if not stopped or blocks:
        raise GatewayError("stream_interrupted")
    if input_count is not None or output_count is not None:
        yield StreamEvent(
            seq,
            request.request_id,
            spec.provider_id,
            spec.model_id,
            "usage",
            usage=TokenUsage(input_count, output_count),
        )
        seq += 1
    yield StreamEvent(
        seq, request.request_id, spec.provider_id, spec.model_id, "complete", finish_reason=finish
    )
