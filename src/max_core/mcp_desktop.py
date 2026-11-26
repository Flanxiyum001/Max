"""Owner-managed desktop MCP profile, discovery and explicit per-tool review.

Configuration contains endpoint or command metadata and pinned schemas only.
Tokens are resolved from environment variables at runtime, never persisted
here. Nothing discovered from a server, remote or local stdio, is executable
until the owner enables that exact observed schema. Remote calls and stdio
calls still need MAX Trust approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from max_core.mcp_discovery import MCPConnection, ObservedTool, discover_tools
from max_core.mcp_policy import ReviewedToolPolicy
from max_core.mcp_policy import RiskTier as ReviewedRiskTier
from max_core.mcp_stdio import (
    StdioBridgeError,
    StdioMCPConnection,
    allowlisted_stdio_connection,
    discover_stdio_tools,
)
from max_core.playwright_mcp import installed_connection

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_MAX_PROFILE_BYTES = 512_000
_MAX_CONNECTIONS = 16
_MAX_ENABLED_TOOLS = 20
_TYPES = {"string", "integer", "number", "boolean", "null"}
_STDIO_ALLOWLIST_ENV = "MAX_MCP_STDIO_BINARIES"


class MCPSettingsError(ValueError):
    """Stable, display-safe configuration or lifecycle error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def default_profile_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    custom = source.get("MAX_MCP_CONFIG_PATH")
    if custom:
        return Path(custom).expanduser()
    if os.name == "nt":
        root = Path(source.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "MAX" / "mcp-servers.json"
    root = Path(source.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "max" / "mcp-servers.json"


class MCPDesktopManager:
    """Host-only configuration API. Never register these operations as model tools."""

    def __init__(self, path: Path | None = None, *, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(os.environ if env is None else env)
        self.path = path if path is not None else default_profile_path(self._env)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        path = self.path
        try:
            if not path.exists():
                if path.is_symlink():
                    raise MCPSettingsError("unsafe_mcp_profile")
                return {"version": 1, "connections": []}
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise MCPSettingsError("unsafe_mcp_profile")
            if info.st_size > _MAX_PROFILE_BYTES:
                raise MCPSettingsError("mcp_profile_too_large")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("connections"), list)
                or len(data["connections"]) > _MAX_CONNECTIONS
            ):
                raise MCPSettingsError("invalid_mcp_profile")
            ids: set[str] = set()
            for item in data["connections"]:
                if not isinstance(item, dict):
                    raise MCPSettingsError("invalid_mcp_profile")
                ident = item.get("id")
                kind = item.get("kind", "endpoint")
                token_env = item.get("token_env", "")
                args = item.get("args", [])
                if (
                    not isinstance(ident, str)
                    or _NAME.fullmatch(ident) is None
                    or ident in ids
                    or not isinstance(token_env, str)
                    or (token_env and _ENV.fullmatch(token_env) is None)
                    or not isinstance(item.get("tools", []), list)
                ):
                    raise MCPSettingsError("invalid_mcp_profile")
                if kind == "stdio":
                    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                        raise MCPSettingsError("invalid_mcp_profile")
                    try:
                        StdioMCPConnection(
                            ident,
                            item.get("command", ""),
                            tuple(args),
                            True,
                            item.get("persistent", False),
                        )
                    except ValueError:
                        raise MCPSettingsError("invalid_mcp_profile") from None
                elif kind == "endpoint":
                    if "args" in item:
                        raise MCPSettingsError("invalid_mcp_profile")
                    MCPConnection(ident, item.get("endpoint", ""), True)
                else:
                    raise MCPSettingsError("invalid_mcp_profile")
                ids.add(ident)
            return data
        except MCPSettingsError:
            raise
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            raise MCPSettingsError("invalid_mcp_profile") from None

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        if len(encoded.encode("utf-8")) > _MAX_PROFILE_BYTES:
            raise MCPSettingsError("mcp_profile_too_large")
        path = self.path
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or tmp.is_symlink():
                raise MCPSettingsError("unsafe_mcp_profile")
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    file.write(encoded)
                    file.flush()
                    os.fsync(file.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            os.replace(tmp, path)
        except MCPSettingsError:
            raise
        except OSError:
            raise MCPSettingsError("mcp_profile_write_failed") from None

    def connections(self) -> tuple[dict[str, Any], ...]:
        """Display-safe list; no URL query strings, bearer tokens, or raw args."""
        with self._lock:
            rows: list[dict[str, Any]] = []
            for item in self._read()["connections"]:
                tools = item.get("tools", [])
                is_stdio = item.get("kind", "endpoint") == "stdio"
                rows.append(
                    {
                        "id": item["id"],
                        "kind": "stdio" if is_stdio else "endpoint",
                        "endpoint": "" if is_stdio else item["endpoint"],
                        "command": item["command"] if is_stdio else "",
                        "persistent": item.get("persistent", False) if is_stdio else False,
                        "token_env": item.get("token_env", ""),
                        "authenticated": bool(
                            is_stdio
                            or (not item.get("token_env", "") or self._env.get(item["token_env"]))
                        ),
                        "tools": tuple(
                            {
                                "name": tool["name"],
                                "sha256": tool["sha256"],
                                "schema_json": tool["schema_json"],
                                "read_only_hint": tool["read_only_hint"],
                                "allowed_arguments": tuple(tool["allowed_arguments"]),
                                "enabled": tool["enabled"],
                            }
                            for tool in tools
                        ),
                    }
                )
            return tuple(rows)

    def _stdio_allowlist(self) -> tuple[str, ...]:
        raw = self._env.get(_STDIO_ALLOWLIST_ENV)
        if not raw:
            raise MCPSettingsError("stdio_not_allowed")
        entries = [entry for entry in raw.split(os.pathsep) if entry]
        if not entries or not all(os.path.isabs(entry) for entry in entries):
            raise MCPSettingsError("stdio_not_allowed")
        return tuple(entries)

    def add_stdio(
        self,
        ident: str,
        command: str,
        args: Sequence[str] = (),
        *,
        discovery_approved: bool = False,
        persistent: bool = False,
    ) -> None:
        """Owner explicitly adds a local stdio server pinned to one allowlisted binary."""
        if (
            not isinstance(ident, str)
            or _NAME.fullmatch(ident) is None
            or not isinstance(discovery_approved, bool)
        ):
            raise MCPSettingsError("invalid_mcp_connection")
        allowed = self._stdio_allowlist()
        try:
            connection = allowlisted_stdio_connection(
                ident,
                command,
                args,
                allowed_commands=allowed,
                discovery_approved=discovery_approved,
                persistent=persistent,
            )
        except StdioBridgeError as error:
            raise MCPSettingsError(error.code) from None
        except ValueError:
            raise MCPSettingsError("invalid_mcp_connection") from None
        with self._lock:
            data = self._read()
            if len(data["connections"]) >= _MAX_CONNECTIONS:
                raise MCPSettingsError("too_many_mcp_connections")
            if any(row["id"] == ident for row in data["connections"]):
                raise MCPSettingsError("duplicate_mcp_connection")
            data["connections"].append(
                {
                    "id": ident,
                    "kind": "stdio",
                    "command": connection.command,
                    "args": list(connection.args),
                    "discovery_approved": connection.discovery_approved,
                    "persistent": connection.persistent,
                    "tools": [],
                }
            )
            self._write(data)

    def add_playwright(self) -> None:
        """Owner-initiated setup; tools stay disabled until discovery and review."""
        try:
            connection = installed_connection(self._env)
        except ValueError as error:
            raise MCPSettingsError(str(error)) from None
        with self._lock:
            data = self._read()
            if any(row["id"] == "playwright" for row in data["connections"]):
                raise MCPSettingsError("duplicate_mcp_connection")
            if len(data["connections"]) >= _MAX_CONNECTIONS:
                raise MCPSettingsError("too_many_mcp_connections")
            data["connections"].append(
                {
                    "id": "playwright",
                    "kind": "stdio",
                    "command": connection.command,
                    "args": list(connection.args),
                    "discovery_approved": True,
                    "persistent": True,
                    "tools": [],
                }
            )
            self._write(data)

    def add(self, ident: str, endpoint: str, token_env: str = "") -> None:
        """Owner explicitly adds an HTTPS server; no discovery on add."""
        if (
            not isinstance(ident, str)
            or _NAME.fullmatch(ident) is None
            or not isinstance(token_env, str)
            or (token_env and _ENV.fullmatch(token_env) is None)
        ):
            raise MCPSettingsError("invalid_mcp_connection")
        try:
            MCPConnection(ident, endpoint, True)
        except ValueError:
            raise MCPSettingsError("invalid_mcp_connection") from None
        with self._lock:
            data = self._read()
            if len(data["connections"]) >= _MAX_CONNECTIONS:
                raise MCPSettingsError("too_many_mcp_connections")
            if any(row["id"] == ident for row in data["connections"]):
                raise MCPSettingsError("duplicate_mcp_connection")
            data["connections"].append(
                {"id": ident, "endpoint": endpoint, "token_env": token_env, "tools": []}
            )
            self._write(data)

    def _connection(self, item: dict[str, Any]) -> MCPConnection:
        token_env = item["token_env"]
        token = self._env.get(token_env) if token_env else None
        if token_env and not token:
            raise MCPSettingsError("mcp_credential_missing")
        try:
            return MCPConnection(item["id"], item["endpoint"], True, token)
        except ValueError:
            raise MCPSettingsError("invalid_mcp_connection") from None

    def _stdio_connection(self, item: dict[str, Any]) -> StdioMCPConnection:
        if item.get("id") == "playwright":
            try:
                connection = installed_connection(self._env)
            except ValueError:
                raise MCPSettingsError("playwright_install_invalid") from None
            if (item.get("command"), item.get("args"), item.get("persistent")) != (
                connection.command,
                list(connection.args),
                True,
            ):
                raise MCPSettingsError("playwright_install_invalid")
            return connection
        allowed = self._stdio_allowlist()
        try:
            return allowlisted_stdio_connection(
                item["id"],
                item["command"],
                item.get("args", []),
                allowed_commands=allowed,
                discovery_approved=bool(item.get("discovery_approved", False)),
                persistent=item.get("persistent", False),
            )
        except StdioBridgeError as error:
            raise MCPSettingsError(error.code) from None
        except ValueError:
            raise MCPSettingsError("invalid_mcp_connection") from None

    def discover(self, ident: str) -> int:
        """Re-discovery invalidates ALL prior approvals, even if schemas match."""
        with self._lock:
            data = self._read()
            item = next((r for r in data["connections"] if r["id"] == ident), None)
            if item is None:
                raise MCPSettingsError("unknown_mcp_connection")
            is_stdio = item.get("kind", "endpoint") == "stdio"
            if is_stdio:
                stdio_connection = self._stdio_connection(item)
            else:
                endpoint_connection = self._connection(item)
        if is_stdio:
            try:
                observed = asyncio.run(discover_stdio_tools(stdio_connection, env_source=self._env))
            except Exception:
                raise MCPSettingsError("mcp_discovery_failed") from None
        else:
            try:
                observed = asyncio.run(discover_tools(endpoint_connection))
            except Exception:
                raise MCPSettingsError("mcp_discovery_failed") from None
        if len(observed) > 128:
            raise MCPSettingsError("too_many_mcp_tools")
        with self._lock:
            data = self._read()
            item = next((r for r in data["connections"] if r["id"] == ident), None)
            if item is None:
                raise MCPSettingsError("mcp_connection_changed")
            if is_stdio:
                if (
                    item.get("kind", "endpoint") != "stdio"
                    or item["command"] != stdio_connection.command
                ):
                    raise MCPSettingsError("mcp_connection_changed")
            elif (
                item.get("kind", "endpoint") == "stdio"
                or item["endpoint"] != endpoint_connection.endpoint
            ):
                raise MCPSettingsError("mcp_connection_changed")
            item["tools"] = [
                {
                    "name": tool.name,
                    "schema_json": tool.schema_json,
                    "sha256": tool.schema_sha256,
                    "read_only_hint": tool.read_only_hint,
                    "allowed_arguments": [],
                    "enabled": False,
                }
                for tool in observed
            ]
            self._write(data)
        return len(observed)

    def review(self, ident: str, name: str, *, enable: bool) -> None:
        """Owner approves the pinned schema; required scalar arguments only."""
        if type(enable) is not bool:
            raise MCPSettingsError("invalid_mcp_review")
        with self._lock:
            data = self._read()
            item = next((r for r in data["connections"] if r["id"] == ident), None)
            if item is None:
                raise MCPSettingsError("unknown_mcp_connection")
            tool = next((r for r in item["tools"] if r["name"] == name), None)
            if tool is None:
                raise MCPSettingsError("unknown_mcp_tool")
            try:
                schema = json.loads(tool["schema_json"])
                canonical = json.dumps(
                    schema, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                if (
                    canonical != tool["schema_json"]
                    or hashlib.sha256(canonical.encode()).hexdigest() != tool["sha256"]
                    or type(tool["read_only_hint"]) is not bool
                ):
                    raise MCPSettingsError("schema_not_reviewed")
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise MCPSettingsError("schema_not_reviewed") from None
            if not isinstance(schema, dict):
                raise MCPSettingsError("schema_not_reviewed")
            props = schema.get("properties", {})
            required = schema.get("required", [])
            if (
                not isinstance(props, dict)
                or not isinstance(required, list)
                or any(
                    not isinstance(key, str)
                    or not isinstance(props.get(key), dict)
                    or props[key].get("type") not in _TYPES
                    for key in props
                )
                or not set(required) <= set(props)
            ):
                raise MCPSettingsError("unsupported_mcp_schema")
            tool["allowed_arguments"] = sorted(props)
            tool["enabled"] = enable
            self._write(data)

    def runtime(
        self,
    ) -> tuple[
        tuple[MCPConnection | StdioMCPConnection, ...],
        tuple[tuple[ObservedTool, ReviewedToolPolicy], ...],
    ]:
        """Return only explicitly enabled, credentialed, digest-pinned entries."""
        with self._lock:
            connections: list[MCPConnection | StdioMCPConnection] = []
            reviewed: list[tuple[ObservedTool, ReviewedToolPolicy]] = []
            for item in self._read()["connections"]:
                approved = [r for r in item["tools"] if r["enabled"]]
                if not approved:
                    continue
                if item.get("kind", "endpoint") == "stdio":
                    connection: MCPConnection | StdioMCPConnection = self._stdio_connection(item)
                else:
                    connection = self._connection(item)
                connections.append(connection)
                for tool in approved:
                    observed = ObservedTool(
                        item["id"],
                        tool["name"],
                        tool["schema_json"],
                        tool["sha256"],
                        tool["read_only_hint"],
                    )
                    policy = ReviewedToolPolicy(
                        "owner",
                        item["id"],
                        tool["name"],
                        tool["sha256"],
                        ReviewedRiskTier.R1 if tool["read_only_hint"] else ReviewedRiskTier.R2,
                        frozenset(tool["allowed_arguments"]),
                        True,
                    )
                    reviewed.append((observed, policy))
            if len(reviewed) > _MAX_ENABLED_TOOLS:
                raise MCPSettingsError("too_many_enabled_mcp_tools")
            return tuple(connections), tuple(reviewed)
