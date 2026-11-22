"""Desktop MCP profile tests: explicit review, credential isolation and fail-closed paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from max_core.mcp_desktop import MCPDesktopManager, MCPSettingsError
from max_core.mcp_discovery import ObservedTool


def _tool(name: str = "search") -> ObservedTool:
    schema = json.dumps(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ObservedTool("github", name, schema, hashlib.sha256(schema.encode()).hexdigest(), True)


def test_server_add_is_offline_and_credentials_are_not_written(tmp_path: Path) -> None:
    path = tmp_path / "mcp-servers.json"
    manager = MCPDesktopManager(path, env={"MAX_MCP_GITHUB_TOKEN": "sensitive-token"})
    manager.add("github", "https://api.githubcopilot.com/mcp/", "MAX_MCP_GITHUB_TOKEN")
    assert "sensitive-token" not in path.read_text()
    assert manager.connections()[0]["authenticated"]
    assert manager.runtime() == ((), ())


def test_discovery_requires_explicit_review_and_pins_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MCPDesktopManager(tmp_path / "profile.json", env={})
    manager.add("github", "https://example.test/mcp")

    async def fake_discovery(_connection: object) -> tuple[ObservedTool, ...]:
        return (_tool(),)

    monkeypatch.setattr("max_core.mcp_desktop.discover_tools", fake_discovery)
    assert manager.discover("github") == 1
    assert manager.runtime() == ((), ())
    assert manager.connections()[0]["tools"][0]["enabled"] is False

    manager.review("github", "search", enable=True)
    connections, reviewed = manager.runtime()
    assert len(connections) == 1
    assert len(reviewed) == 1
    assert reviewed[0][0].schema_sha256 == _tool().schema_sha256
    assert reviewed[0][1].allowed_arguments == frozenset({"query", "limit"})

    # A second discovery never inherits old permissions.
    manager.discover("github")
    assert manager.runtime() == ((), ())


def test_missing_token_blocks_enabled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MCPDesktopManager(tmp_path / "profile.json", env={})
    manager.add("github", "https://example.test/mcp", "MAX_MCP_GITHUB_TOKEN")
    # No network; just simulate a reviewed local configuration.
    data = manager._read()
    tool = _tool()
    data["connections"][0]["tools"] = [
        {
            "name": tool.name,
            "schema_json": tool.schema_json,
            "sha256": tool.schema_sha256,
            "read_only_hint": True,
            "allowed_arguments": ["query", "limit"],
            "enabled": True,
        }
    ]
    manager._write(data)
    with pytest.raises(MCPSettingsError, match="mcp_credential_missing"):
        manager.runtime()


def test_profile_rejects_symlink_and_duplicate_connections(tmp_path: Path) -> None:
    profile = tmp_path / "config.json"
    manager = MCPDesktopManager(profile, env={})
    manager.add("github", "https://example.test/mcp")
    with pytest.raises(MCPSettingsError, match="duplicate_mcp_connection"):
        manager.add("github", "https://example.test/mcp")
    link = tmp_path / "linked.json"
    link.symlink_to(profile)
    with pytest.raises(MCPSettingsError, match="unsafe_mcp_profile"):
        MCPDesktopManager(link, env={}).connections()


def test_unsupported_nested_schema_cannot_be_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MCPDesktopManager(tmp_path / "profile.json", env={})
    manager.add("github", "https://example.test/mcp")
    schema = json.dumps(
        {"type": "object", "properties": {"payload": {"type": "object"}}},
        sort_keys=True,
        separators=(",", ":"),
    )

    async def fake(_connection: object) -> tuple[ObservedTool, ...]:
        return (
            ObservedTool("github", "write", schema, hashlib.sha256(schema.encode()).hexdigest()),
        )

    monkeypatch.setattr("max_core.mcp_desktop.discover_tools", fake)
    manager.discover("github")
    with pytest.raises(MCPSettingsError, match="unsupported_mcp_schema"):
        manager.review("github", "write", enable=True)
    assert manager.runtime() == ((), ())
