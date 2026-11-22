"""Integration coverage: live desktop MCP routing and single-use Trust decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from max_core.assistant_session import AssistantSession
from max_core.mcp_desktop import MCPDesktopManager
from max_core.mcp_discovery import ObservedTool
from max_core.mcp_execute import MCPToolExecutor
from max_core.native_desktop.backend import NativeDesktopCore
from max_core.tools import ToolError


def test_native_core_routes_reviewed_mcp_through_owner_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_url = f"sqlite+pysqlite:///{(tmp_path / 'max.sqlite3').as_posix()}"
    manager = MCPDesktopManager(tmp_path / "mcp-servers.json", env={})
    manager.add("github", "https://example.test/mcp")
    schema = json.dumps(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    async def fake_discover(_connection: object) -> tuple[ObservedTool, ...]:
        return (
            ObservedTool(
                "github", "search", schema, hashlib.sha256(schema.encode()).hexdigest(), True
            ),
        )

    monkeypatch.setattr("max_core.mcp_desktop.discover_tools", fake_discover)
    calls: list[dict[str, object]] = []

    def fake_remote(_self: MCPToolExecutor, _entry: object, arguments: dict[str, object]) -> str:
        calls.append(arguments)
        return "GitHub fixture result"

    monkeypatch.setattr(MCPToolExecutor, "_dispatch_remote", fake_remote)

    core = NativeDesktopCore(database_url=sqlite_url)
    core._mcp_manager = manager
    try:
        assert "mcp.github.search" not in {x.name for x in core.tool_definitions()}
        assert core.mcp_discover("github") == 1
        assert "mcp.github.search" not in {x.name for x in core.tool_definitions()}
        core.mcp_review("github", "search", enable=True)
        assert "mcp.github.search" in {x.name for x in core.tool_definitions()}

        proposed = core.execute_tool("mcp.github.search", {"query": "owner/repo"})
        assert proposed.status == "awaiting_approval"
        assert proposed.approval_id
        assert not calls

        decided = core.decide_approval_with_continuation(proposed.approval_id, True)
        assert decided.approval["state"] == "approved"
        assert decided.tool_summary is not None
        assert decided.tool_summary.status == "completed"
        assert calls == [{"query": "owner/repo"}]

        # Disabling tools invalidates the composite executor for future calls.
        core.mcp_review("github", "search", enable=False)
        assert "mcp.github.search" not in {x.name for x in core.tool_definitions()}
        denied = core.execute_tool("mcp.github.search", {"query": "owner/repo"})
        assert denied.status == "denied"
        assert not calls[1:]
        with pytest.raises(ToolError, match="mcp_tool_revoked"):
            AssistantSession._execute_approved_mcp(
                core, "mcp.github.search", {"query": "owner/repo"}
            )
        assert len(calls) == 1
    finally:
        core.close()
