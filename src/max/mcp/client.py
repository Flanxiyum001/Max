"""Minimal MCP (Model Context Protocol) client over stdio.

Implements just enough of the 2024-11-05 protocol for MAX: ``initialize``,
``notifications/initialized``, ``tools/list`` and ``tools/call``. Transport is
newline-delimited JSON over the child process' stdin/stdout.
"""

from __future__ import annotations

import json
import select
import subprocess
import time
from typing import Any


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, command: list[str], cwd: str | None = None, env: dict | None = None):
        if not command:
            raise MCPError("empty MCP command")
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._pending: dict[int, dict] = None  # type: ignore[assignment]
        self.server_info: dict = {}

    # ------------------------------------------------------------ lifecycle
    def start(self, timeout: float = 10.0) -> dict:
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=self.env,
            )
        except OSError as exc:
            raise MCPError(f"failed to start MCP server: {exc}") from None
        info = self.initialize(timeout=timeout)
        self.notify("notifications/initialized")
        return info

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- protocol
    def request(self, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("MCP server is not running")
        self._id += 1
        req_id = self._id
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self._write(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._read(deadline - time.monotonic())
            if message is None:
                break
            if message.get("id") == req_id:
                if "error" in message:
                    err = message["error"]
                    raise MCPError(f"{method} failed: {err.get('code')} {err.get('data') or err.get('message')}")
                return message.get("result")
        raise MCPError(f"timeout waiting for {method}")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self, timeout: float = 10.0) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "max-assistant", "version": "0.2.0"},
            },
            timeout=timeout,
        )
        self.server_info = result or {}
        return self.server_info

    def list_tools(self, timeout: float = 10.0) -> list[dict]:
        result = self.request("tools/list", {}, timeout=timeout)
        return list((result or {}).get("tools", []))

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 15.0) -> str:
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )
        if (result or {}).get("isError"):
            raise MCPError(f"tool {name} returned an error")
        parts = []
        for block in (result or {}).get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------- transport
    def _write(self, payload: dict) -> None:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise MCPError("MCP server closed the connection") from None

    def _read(self, timeout: float) -> dict | None:
        assert self.proc and self.proc.stdout
        ready, _, _ = select.select([self.proc.stdout], [], [], max(0.0, timeout))
        if not ready:
            return None
        line = self.proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return self._read(timeout)
        try:
            return json.loads(line)
        except ValueError:
            return self._read(timeout)


def register_mcp_tools(registry, client: MCPClient, prefix: str = "mcp") -> list[str]:
    """Expose MCP tools as MAX tool specs under ``mcp/<server>/<tool>``."""
    from ..core.tools import ToolContext, ToolSpec  # local import to avoid cycles

    server = str((client.server_info.get("serverInfo") or {}).get("name", "server"))
    registered = []
    for tool in client.list_tools():
        full_name = f"{prefix}/{server}/{tool['name']}"

        def make(tool_name: str):
            def handler(args: dict, ctx: ToolContext) -> str:
                return client.call_tool(tool_name, args)

            return handler

        registry.register(
            ToolSpec(
                name=full_name,
                description=str(tool.get("description") or f"MCP tool {tool['name']}"),
                parameters=tool.get("inputSchema")
                or {"type": "object", "properties": {}},
                category="mcp",
                handler=make(tool["name"]),
            )
        )
        registered.append(full_name)
    return registered
