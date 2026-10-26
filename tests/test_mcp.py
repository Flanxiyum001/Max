"""MCP integration: real stdio client against the mock MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from max.mcp import MCPClient, MCPError, register_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


@pytest.fixture
def client():
    c = MCPClient([sys.executable, str(SERVER)])
    info = c.start(timeout=10)
    yield c, info
    c.close()


def test_initialize_handshake(client):
    c, info = client
    assert info["protocolVersion"] == "2024-11-05"
    assert info["serverInfo"]["name"] == "max-mock-mcp"


def test_list_tools(client):
    c, _ = client
    tools = c.list_tools()
    assert {t["name"] for t in tools} == {"echo", "add"}
    assert tools[0]["inputSchema"]["type"] == "object"


def test_call_tool(client):
    c, _ = client
    assert c.call_tool("echo", {"text": "ping"}) == "echo: ping"
    assert c.call_tool("add", {"a": 2, "b": 3}) == "5"


def test_unknown_tool_raises(client):
    c, _ = client
    with pytest.raises(MCPError):
        c.call_tool("nope", {})


def test_mcp_tools_are_gated_by_policy(agent, tmp_config):
    """Registered MCP tools flow through MAX's permission policy."""
    client = MCPClient([sys.executable, str(SERVER)])
    client.start(timeout=10)
    try:
        names = register_mcp_tools(agent.registry, client)
        assert names == ["mcp/max-mock-mcp/echo", "mcp/max-mock-mcp/add"]

        agent.provider.__class__  # sanity
        from max.providers import MockProvider

        agent.provider = MockProvider(
            script=[
                {
                    "tool_calls": [
                        {"name": "mcp/max-mock-mcp/echo", "arguments": {"text": "hi-mcp"}}
                    ]
                },
                {"text": "server said hi-mcp"},
            ]
        )
        reply = agent.ask("use the mcp echo tool")
        assert reply.ok
        assert reply.tool_events[0]["allowed"] is True
        tool_msgs = [m for m in agent.provider.requests[-1]["messages"] if m["role"] == "tool"]
        assert "echo: hi-mcp" in tool_msgs[0]["content"]
    finally:
        client.close()


def test_closed_client_errors_cleanly():
    c = MCPClient([sys.executable, str(SERVER)])
    c.start()
    c.close()
    with pytest.raises(MCPError, match="not running"):
        c.list_tools()


def test_failed_spawn_reports_error():
    with pytest.raises(MCPError):
        MCPClient(["/nonexistent/binary"]).start(timeout=2)
