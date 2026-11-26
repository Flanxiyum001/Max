"""Allowlisted local stdio MCP bridge: no shell, scrubbed environment, Trust-gated.

Local stdio servers are child processes, so admission is stricter than remote
HTTPS: the command must be a host-provided absolute allowlist entry resolved
once by trusted configuration, arguments are fixed validated strings, and the
child environment is stripped to a small safe set so credentials are never
inherited. Discovery and execution reuse the same bounded observation rules as
remote MCP; every tool execution still requires a fresh single-use owner
approval through MAX Trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from max_core.bounded_commands import ENV_KEYS
from max_core.mcp_discovery import DiscoveryError, ObservedTool

_STDIO_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_ARGS = 8
_MAX_ARG_CHARS = 256
_MAX_TOOLS = 512
_MAX_PAGES = 16
_DEADLINE_SECONDS = 30


class StdioBridgeError(ValueError):
    """Fixed safe codes only; never embeds raw command output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(ch) < 33 for ch in value) and value.strip() != value:
        return None
    if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
        return None
    return value


@dataclass(frozen=True)
class StdioMCPConnection:
    """Host-configured local server; the exact binary is pinned, never model-chosen."""

    connection_id: str
    command: str
    args: tuple[str, ...] = ()
    discovery_approved: bool = False
    persistent: bool = False

    def __post_init__(self) -> None:
        command = _clean_text(self.command, 256)
        valid = (
            isinstance(self.connection_id, str)
            and _STDIO_ID.fullmatch(self.connection_id) is not None
            and command is not None
            and os.path.isabs(command)
            and ".." not in command.replace("\\", "/").split("/")
            and type(self.discovery_approved) is bool
            and type(self.persistent) is bool
            and isinstance(self.args, tuple)
            and len(self.args) <= _MAX_ARGS
        )
        if valid:
            for arg in self.args:
                if _clean_text(arg, _MAX_ARG_CHARS) is None:
                    valid = False
                    break
        if not valid:
            raise DiscoveryError("invalid_mcp_stdio_connection")


def allowlisted_stdio_connection(
    connection_id: str,
    command: str,
    args: Sequence[str] = (),
    *,
    allowed_commands: Sequence[str],
    discovery_approved: bool = False,
    persistent: bool = False,
) -> StdioMCPConnection:
    """Resolve the host allowlist once; a bare name may only denote an allowlisted path."""
    if not allowed_commands:
        raise StdioBridgeError("stdio_not_allowed")
    candidates = {entry: entry for entry in allowed_commands}
    requested = _clean_text(command, 256)
    resolved = candidates.get(requested or "")
    if resolved is None and requested is not None and os.path.basename(requested) == requested:
        resolved = candidates.get(os.path.basename(requested))
    if resolved is None:
        raise StdioBridgeError("stdio_command_not_allowed")
    return StdioMCPConnection(
        connection_id,
        resolved,
        tuple(args),
        discovery_approved,
        persistent,
    )


def stdio_parameters(
    connection: StdioMCPConnection, *, env_source: Mapping[str, str] | None = None
) -> StdioServerParameters:
    """Scratch environment: only known-safe desktop keys reach the child."""
    if not isinstance(connection, StdioMCPConnection):
        raise DiscoveryError("invalid_mcp_stdio_connection")
    source = os.environ if env_source is None else env_source
    env = {key: source[key] for key in ENV_KEYS if key in source}
    return StdioServerParameters(
        command=connection.command,
        args=list(connection.args),
        env=env,
        cwd=None,
    )


def _observe(tool: Any, connection_id: str, names: set[str]) -> ObservedTool:
    if not isinstance(tool.inputSchema, dict) or tool.name in names:
        raise DiscoveryError("invalid_mcp_tool_identity")
    if not _clean_text(tool.name, 512) or tool.name != tool.name.strip() or len(tool.name) > 128:
        raise DiscoveryError("invalid_mcp_tool_identity")
    schema = json.dumps(tool.inputSchema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(schema.encode()) > 65536:
        raise DiscoveryError("mcp_schema_too_large")
    names.add(tool.name)
    return ObservedTool(
        connection_id,
        tool.name,
        schema,
        hashlib.sha256(schema.encode()).hexdigest(),
        bool(getattr(tool.annotations, "readOnlyHint", False)),
    )


async def discover_stdio_tools(
    connection: StdioMCPConnection,
    *,
    env_source: Mapping[str, str] | None = None,
) -> tuple[ObservedTool, ...]:
    """Bounded stdio discovery: initialize, list tools, close; nothing executes."""
    if not isinstance(connection, StdioMCPConnection):
        raise DiscoveryError("invalid_mcp_stdio_connection")
    if connection.discovery_approved is not True:
        raise DiscoveryError("mcp_discovery_not_approved")
    observed: list[ObservedTool] = []
    names: set[str] = set()
    cursors: set[str] = set()
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, anyio.fail_after(_DEADLINE_SECONDS):
            async with stdio_client(
                stdio_parameters(connection, env_source=env_source),
                errlog=sink,
            ) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=10)
                ) as session:
                    initialized = await session.initialize()
                    if initialized.capabilities.tools is None:
                        return ()
                    cursor: str | None = None
                    for _page in range(_MAX_PAGES):
                        page = await session.list_tools(cursor=cursor)
                        for tool in page.tools:
                            observed.append(_observe(tool, connection.connection_id, names))
                            if len(observed) > _MAX_TOOLS:
                                raise DiscoveryError("too_many_mcp_tools")
                        cursor = page.nextCursor
                        if cursor is None:
                            return tuple(observed)
                        if not _clean_text(cursor, 2048) or cursor in cursors:
                            raise DiscoveryError("invalid_mcp_cursor")
                        cursors.add(cursor)
                    raise DiscoveryError("too_many_mcp_pages")
    except DiscoveryError:
        raise
    except Exception:
        # The child may emit arbitrary bytes; never surface them to callers.
        raise DiscoveryError("mcp_stdio_failed") from None
