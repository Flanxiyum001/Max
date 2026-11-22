"""Shared bounded MCP egress transport: pinned endpoint, no redirects, bounded bytes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

MAX_BYTES = 1_000_000


class McpEgressError(ValueError):
    """Fixed safe egress code; never wraps raw endpoint or payload data."""


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_BYTES:
                raise McpEgressError("mcp_response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class EndpointTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str, transport: httpx.AsyncBaseTransport) -> None:
        self.endpoint = httpx.URL(endpoint)
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.endpoint or request.method not in {"POST", "GET", "DELETE"}:
            raise McpEgressError("mcp_egress_denied")
        response = await self.transport.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise McpEgressError("mcp_redirect_denied")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise McpEgressError("mcp_compressed_response_denied")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise McpEgressError("invalid_mcp_stream")
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
