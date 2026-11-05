"""Explicitly configured HTTP adapters. No automatic provider discovery or credentials."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from max_core.gateway.contracts import Dialect, GatewayError, GatewayRequest, GatewayResponse, ModelSpec, StreamEvent
from max_core.gateway.normalize import normalize_anthropic, normalize_openai
from max_core.gateway.streaming import anthropic_events, openai_events, sse_frames

MAX_RESPONSE_BYTES = 1_000_000


def _messages(request: GatewayRequest, dialect: Dialect) -> dict[str, Any]:
    if dialect == Dialect.OPENAI_CHAT:
        return {"messages": [{"role": m.role, "content": m.content} for m in request.messages]}
    system_parts = [m.content for m in request.messages if m.role == "system"]
    regular = [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"]
    if not regular:
        raise GatewayError("invalid_request")
    result: dict[str, Any] = {"messages": regular}
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    return result


def _params(spec: ModelSpec, request: GatewayRequest, *, stream: bool) -> dict[str, Any]:
    data = _messages(request, spec.dialect)
    data["model"] = spec.model_id
    data["stream"] = stream
    if spec.dialect == Dialect.OPENAI_CHAT:
        # Compatibility baseline. Not every OpenAI-like endpoint supports newer fields.
        data["max_tokens"] = request.max_output_tokens
        if stream:
            data["stream_options"] = {"include_usage": True}
    else:
        data["max_tokens"] = request.max_output_tokens
    return data


def _headers(spec: ModelSpec, *, stream: bool) -> dict[str, str]:
    result = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    if spec.dialect == Dialect.OPENAI_CHAT:
        if spec.api_key is not None:
            result["Authorization"] = f"Bearer {spec.api_key}"
    else:
        result["anthropic-version"] = "2023-06-01"
        if spec.api_key is not None:
            result["x-api-key"] = spec.api_key
    return result


def _check_status(status: int) -> None:
    if status in {429, 503}:
        raise GatewayError("rate_limited" if status == 429 else "provider_unavailable", fallback_safe=True)
    if status in {401, 403}:
        raise GatewayError("auth_failure")
    if status == 400 or status == 422:
        raise GatewayError("invalid_request")
    if status >= 500:
        raise GatewayError("provider_unavailable")
    if not 200 <= status < 300:
        raise GatewayError("provider_error")


class HTTPAdapter:
    """Uses an injected HTTPX client. Redirects are never followed with credentials."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def complete(self, spec: ModelSpec, request: GatewayRequest) -> GatewayResponse:
        try:
            async with self.client.stream(
                "POST", spec.endpoint, json=_params(spec, request, stream=False),
                headers=_headers(spec, stream=False), timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as response:
                _check_status(response.status_code)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise GatewayError("response_too_large")
                    chunks.extend(chunk)
        except httpx.TimeoutException as exc:
            raise GatewayError("timeout") from exc
        except httpx.RequestError as exc:
            raise GatewayError("provider_unavailable") from exc
        try:
            data = json.loads(chunks)
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("malformed_provider_response") from exc
        if not isinstance(data, dict):
            raise GatewayError("malformed_provider_response")
        if data.get("model") is not None and data["model"] != spec.model_id:
            raise GatewayError("model_mismatch")
        if spec.dialect == Dialect.OPENAI_CHAT:
            return normalize_openai(data, request.request_id, spec)
        return normalize_anthropic(data, request.request_id, spec)

    async def stream(self, spec: ModelSpec, request: GatewayRequest) -> AsyncIterator[StreamEvent]:
        try:
            async with self.client.stream(
                "POST", spec.endpoint, json=_params(spec, request, stream=True),
                headers=_headers(spec, stream=True), timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as response:
                _check_status(response.status_code)
                frames = sse_frames(response.aiter_lines())
                if spec.dialect == Dialect.OPENAI_CHAT:
                    async for event in openai_events(frames, request, spec):
                        yield event
                else:
                    async for event in anthropic_events(frames, request, spec):
                        yield event
        except httpx.TimeoutException as exc:
            raise GatewayError("timeout") from exc
        except httpx.RequestError as exc:
            raise GatewayError("stream_interrupted") from exc
