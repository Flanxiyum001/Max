"""MCP backend: one internal interface, pluggable transport.

MAX's core only ever sees the stable surface here (list tools / call tool),
so the existing stdio client and the official MCP Python SDK can be swapped
behind the same boundary without touching the agent loop.

Per-server policy is explicit: a server is disabled by default, inherits a
whitelisted environment only, and *no* tool is enabled automatically —
``tool_allow`` decides, ``approval_required`` adds a second gate, and every
call is audited. Untrusted tool descriptions or results never change policy.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from ..core.tools import ToolError, find_injection_markers

_SECRET_NAME = re.compile(r"(?i)(_KEY|_TOKEN|_SECRET|_PASSWD|_PASSWORD|_CREDENTIAL|_COOKIE|^AUTH)")
_MAX_ARGS_CHARS = 4000


@dataclass
class MCPServerConfig:
    """Explicit configuration for one MCP server."""

    id: str
    command: list[str]
    enabled: bool = False
    env_allow: tuple[str, ...] = ()
    timeout_s: float = 10.0
    tool_allow: tuple[str, ...] = ()
    tool_deny: tuple[str, ...] = ()
    approval_required: tuple[str, ...] = ()
    cwd: str | None = None

    def redacted(self) -> dict:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "command": list(self.command),
            "env_allow": list(self.env_allow),
            "timeout_s": self.timeout_s,
            "tool_allow": list(self.tool_allow),
            "tool_deny": list(self.tool_deny),
            "approval_required": list(self.approval_required),
        }


def parse_server_configs(raw: str | None) -> list[MCPServerConfig]:
    """Parse ``MAX_MCP_SERVERS`` (a JSON array). Invalid input disables MCP."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    configs: list[MCPServerConfig] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "").strip()
        command = entry.get("command")
        if not sid or not isinstance(command, list) or not all(isinstance(c, str) for c in command):
            continue
        configs.append(
            MCPServerConfig(
                id=sid,
                command=list(command),
                enabled=bool(entry.get("enabled", False)),
                env_allow=tuple(entry.get("env_allow") or ()),
                timeout_s=float(entry.get("timeout_s") or 10.0),
                tool_allow=tuple(entry.get("tool_allow") or ()),
                tool_deny=tuple(entry.get("tool_deny") or ()),
                approval_required=tuple(entry.get("approval_required") or ()),
                cwd=entry.get("cwd"),
            )
        )
    return configs


def build_env(config: MCPServerConfig) -> dict[str, str]:
    """Only whitelisted, non-secret-looking variables reach a server process."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    for name in config.env_allow:
        if _SECRET_NAME.search(name):
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


class MCPServerManager:
    """Lifecycle + permission boundary for configured MCP servers."""

    def __init__(self, configs: list[MCPServerConfig] | None = None, audit=None, client_factory=None):
        self.configs = {c.id: c for c in (configs or [])}
        self.audit = audit
        self._client_factory = client_factory  # injectable for tests
        self._clients: dict[str, object] = {}
        self._tools: dict[str, list[dict]] = {}

    # ------------------------------------------------------------ lifecycle
    def start(self, server_id: str) -> dict:
        config = self.configs.get(server_id)
        if config is None:
            raise ToolError(f"unknown MCP server: {server_id}")
        if not config.enabled:
            raise ToolError(f"MCP server '{server_id}' is disabled by configuration")
        if server_id in self._clients:
            return {"id": server_id, "state": "running"}
        factory = self._client_factory or _default_client_factory
        try:
            client = factory(config)
            info = client.start(timeout=config.timeout_s)
            tools = client.list_tools(timeout=config.timeout_s)
        except Exception as exc:  # noqa: BLE001 - a broken server must not crash MAX
            self._record("mcp_start_failed", server=server_id, error=exc.__class__.__name__)
            raise ToolError(f"MCP server '{server_id}' failed to start: {exc}") from None
        self._clients[server_id] = client
        self._tools[server_id] = list(tools or [])
        self._record("mcp_started", server=server_id, tools=len(self._tools[server_id]))
        return {"id": server_id, "state": "running", "server_info": info, "tools": len(self._tools[server_id])}

    def stop(self, server_id: str) -> dict:
        client = self._clients.pop(server_id, None)
        self._tools.pop(server_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._record("mcp_stopped", server=server_id)
        return {"id": server_id, "state": "stopped"}

    def stop_all(self) -> None:
        for sid in list(self._clients):
            self.stop(sid)

    # ----------------------------------------------------------- discovery
    def list_tools(self, server_id: str) -> list[dict]:
        config = self._configs_or_raise(server_id)
        if server_id not in self._tools:
            self.start(server_id)
        tools = self._tools.get(server_id, [])
        # Only explicitly allowed tools are ever visible to the model.
        visible = [t for t in tools if str(t.get("name")) in config.tool_allow]
        denied = [str(t.get("name")) for t in tools if str(t.get("name")) in config.tool_deny]
        return [t for t in visible if str(t.get("name")) not in denied]

    def enabled_tools(self) -> list[str]:
        names = []
        for sid, config in self.configs.items():
            if config.enabled:
                names.extend(f"mcp/{sid}/{t}" for t in config.tool_allow if t not in config.tool_deny)
        return sorted(set(names))

    # -------------------------------------------------------------- calling
    def call(
        self,
        server_id: str,
        tool: str,
        arguments: dict | None = None,
        approved: bool = False,
    ) -> str:
        config = self._configs_or_raise(server_id)
        if not config.enabled:
            raise ToolError(f"MCP server '{server_id}' is disabled by configuration")
        if tool not in config.tool_allow:
            self._record("mcp_rejected", server=server_id, tool=tool, reason="not_allowlisted")
            raise ToolError(f"tool '{tool}' is not enabled for server '{server_id}'")
        if tool in config.tool_deny:
            self._record("mcp_rejected", server=server_id, tool=tool, reason="denied")
            raise ToolError(f"tool '{tool}' is denied for server '{server_id}'")
        if tool in config.approval_required and not approved:
            self._record("mcp_rejected", server=server_id, tool=tool, reason="approval_required")
            raise ToolError(f"tool '{tool}' requires human approval before it can run")

        args = arguments if isinstance(arguments, dict) else {}
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be an object")
        if len(json.dumps(args, default=str)) > _MAX_ARGS_CHARS:
            raise ToolError("tool arguments exceed the size limit")

        if server_id not in self._clients:
            self.start(server_id)
        client = self._clients[server_id]
        try:
            result = client.call_tool(tool, args, timeout=config.timeout_s)
        except Exception as exc:  # noqa: BLE001
            self._record("mcp_error", server=server_id, tool=tool, error=exc.__class__.__name__)
            raise ToolError(f"MCP tool '{tool}' failed: {exc}") from None

        text = str(result)
        flagged = find_injection_markers(text)
        if flagged:
            self._record("mcp_injection_flagged", server=server_id, tool=tool, patterns=flagged)
            text = "[max: untrusted MCP output flagged — possible prompt injection]\n" + text
        self._record(
            "mcp_call",
            server=server_id,
            tool=tool,
            approved=approved,
            chars=min(len(text), 500),
            flagged=bool(flagged),
        )
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"
        return text

    # -------------------------------------------------------------- helpers
    def _configs_or_raise(self, server_id: str) -> MCPServerConfig:
        config = self.configs.get(str(server_id or "").strip())
        if config is None:
            raise ToolError(f"unknown MCP server: {server_id}")
        return config

    def _record(self, event: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(event, **detail)

    def status(self) -> list[dict]:
        return [
            {**c.redacted(), "running": c.id in self._clients}
            for c in self.configs.values()
        ]


def _default_client_factory(config: MCPServerConfig):
    from .client import MCPClient

    return MCPClient(config.command, cwd=config.cwd, env=build_env(config))


def register_mcp_manager_tools(registry, manager: MCPServerManager) -> None:
    """Expose discovery/status through the standard tool registry."""
    from ..core.tools import ToolContext, ToolSpec

    def _list(args: dict, ctx: ToolContext) -> str:
        sid = str(args.get("server", ""))
        tools = manager.list_tools(sid)
        return json.dumps(
            {
                "server": sid,
                "tools": [
                    {
                        "name": t.get("name"),
                        "description": str(t.get("description", ""))[:200],
                        "inputSchema": t.get("inputSchema"),
                    }
                    for t in tools
                ],
            },
            indent=2,
        )

    def _status(args: dict, ctx: ToolContext) -> str:
        return json.dumps(manager.status(), indent=2)

    registry.register(
        ToolSpec(
            name="mcp_list_tools",
            description="List only the explicitly enabled tools of a configured MCP server.",
            parameters={
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "required": ["server"],
            },
            category="mcp",
            handler=_list,
        )
    )
    registry.register(
        ToolSpec(
            name="mcp_status",
            description="Show configured MCP servers, their enabled state and which tools are enabled.",
            parameters={"type": "object", "properties": {}},
            category="mcp",
            handler=_status,
        )
    )
