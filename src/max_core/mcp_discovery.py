"""Explicitly configured MCP discovery. Observations never grant tool execution.

Library only: no API, environment loading, plugin installation or tool calls.
The host must supply trusted endpoint configuration and network egress controls.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
MAX_BYTES = 1_000_000
MAX_TOOLS = 512
MAX_PAGES = 16


class DiscoveryError(ValueError):
    """Fixed error codes only; upstream messages may contain credentials."""


@dataclass(frozen=True)
class MCPConnection:
    connection_id: str
    endpoint: str = field(repr=False)
    discovery_approved: bool = False
    bearer_token: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.endpoint)
            valid = (
                bool(_NAME.fullmatch(self.connection_id))
                and parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and len(self.endpoint) <= 2048
                and not any(ord(c) < 33 or ord(c) > 126 for c in self.endpoint)
                and parsed.port in (None, 443)
                and type(self.discovery_approved) is bool
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise DiscoveryError("invalid_mcp_connection")
        if self.bearer_token is not None and (
            not self.bearer_token
            or len(self.bearer_token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in self.bearer_token)
        ):
            raise DiscoveryError("invalid_mcp_credential")


@dataclass(frozen=True)
class MCPRegistry:
    """Host-owned registry. Plugins and discovery cannot register themselves."""

    connections: tuple[MCPConnection, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.connections, tuple) or len(self.connections) > 64:
            raise DiscoveryError("invalid_mcp_registry")
        if any(not isinstance(item, MCPConnection) for item in self.connections):
            raise DiscoveryError("invalid_mcp_registry")
        identities = [item.connection_id for item in self.connections]
        if len(identities) != len(set(identities)):
            raise DiscoveryError("duplicate_mcp_connection")

    def resolve(self, connection_id: str) -> MCPConnection:
        for connection in self.connections:
            if connection.connection_id == connection_id:
                return connection
        raise DiscoveryError("unknown_mcp_connection")


@dataclass(frozen=True)
class ObservedTool:
    connection_id: str
    name: str
    schema_json: str
    schema_sha256: str
    # There is intentionally no executable/approved/read-only flag. A server's
    # readOnlyHint is untrusted and cannot establish a MAX permission.


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_BYTES:
                raise DiscoveryError("mcp_response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class _EndpointTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str, transport: httpx.AsyncBaseTransport) -> None:
        self.endpoint = httpx.URL(endpoint)
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.endpoint or request.method not in {"POST", "GET", "DELETE"}:
            raise DiscoveryError("mcp_egress_denied")
        response = await self.transport.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise DiscoveryError("mcp_redirect_denied")
        # Bound bytes before HTTPX decoding: a server can ignore identity and
        # otherwise expand a small compressed body beyond the response budget.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise DiscoveryError("mcp_compressed_response_denied")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise DiscoveryError("invalid_mcp_stream")
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


async def discover_tools(
    connection: MCPConnection,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[ObservedTool, ...]:
    """Initialize, list bounded tool metadata and close one reviewed connection.

    Optional transport is a trusted host injection for tests or egress enforcement,
    never an option exposed to a model. Caller owns authentication/consent and
    must not derive MCPConnection from model output or plugin manifests.
    No retries, OAuth redirects, resources/read, prompts/get or tools/call.
    """
    if connection.discovery_approved is not True:
        raise DiscoveryError("mcp_discovery_not_approved")
    headers = {"Accept-Encoding": "identity"}
    if connection.bearer_token is not None:
        headers["Authorization"] = "Bearer " + connection.bearer_token
    observed: list[ObservedTool] = []
    names: set[str] = set()
    cursors: set[str] = set()
    try:
        # A whole-operation deadline also bounds SSE streams and pagination.
        with anyio.fail_after(30):
            async with httpx.AsyncClient(
                transport=_EndpointTransport(
                    connection.endpoint, transport or httpx.AsyncHTTPTransport(retries=0)
                ),
                headers=headers,
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with streamable_http_client(connection.endpoint, http_client=client) as (
                    reader,
                    writer,
                    _,
                ):
                    async with ClientSession(
                        reader, writer, read_timeout_seconds=timedelta(seconds=10)
                    ) as session:
                        initialized = await session.initialize()
                        if initialized.capabilities.tools is None:
                            return ()
                        cursor: str | None = None
                        for _page in range(MAX_PAGES):
                            page = await session.list_tools(cursor=cursor)
                            for tool in page.tools:
                                if not _NAME.fullmatch(tool.name) or tool.name in names:
                                    raise DiscoveryError("invalid_mcp_tool_identity")
                                if tool.inputSchema.get("type") != "object":
                                    raise DiscoveryError("invalid_mcp_tool_schema")
                                schema = json.dumps(
                                    tool.inputSchema,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                )
                                if len(schema.encode()) > 65536:
                                    raise DiscoveryError("mcp_schema_too_large")
                                names.add(tool.name)
                                observed.append(
                                    ObservedTool(
                                        connection.connection_id,
                                        tool.name,
                                        schema,
                                        hashlib.sha256(schema.encode()).hexdigest(),
                                    )
                                )
                                if len(observed) > MAX_TOOLS:
                                    raise DiscoveryError("too_many_mcp_tools")
                            cursor = page.nextCursor
                            if cursor is None:
                                return tuple(observed)
                            if not cursor or len(cursor) > 2048 or cursor in cursors:
                                raise DiscoveryError("invalid_mcp_cursor")
                            cursors.add(cursor)
                        raise DiscoveryError("too_many_mcp_pages")
    except Exception:
        # SDK task groups can wrap errors in exception groups containing raw
        # URLs/payloads. Do not expose those nested exceptions to callers.
        raise DiscoveryError("mcp_discovery_failed") from None
