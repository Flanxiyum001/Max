"""Regression checks for the owner-selected hosted GitHub MCP catalogue."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest

from max_core.github_tool_catalogue import (
    GITHUB_MCP_ENDPOINT,
    GITHUB_READS,
    GITHUB_TOOLS,
    GITHUB_TOOL_GUIDANCE,
    GitHubSchemaError,
    github_headers,
    validate_github_arguments,
    validate_github_operation,
)
from max_core.mcp_discovery import DiscoveryError, MCPConnection, ObservedTool
from max_core.tools import ToolError, ToolRisk
from max_core.web.github_connect import WebGitHubConnector, _Pending, _reviewable


def observed(name: str, schema: dict[str, Any]) -> ObservedTool:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return ObservedTool("github", name, encoded, hashlib.sha256(encoded.encode()).hexdigest())


def test_explicit_catalogue_requests_all_46_tools() -> None:
    assert len(GITHUB_TOOLS) == 46
    assert set(GITHUB_TOOL_GUIDANCE) == GITHUB_TOOLS
    assert "ui_get" in GITHUB_READS
    assert "get_me" in GITHUB_READS
    assert "merge_pull_request" not in GITHUB_READS
    headers = github_headers()
    assert set(headers["X-MCP-Tools"].split(",")) == GITHUB_TOOLS
    assert headers["X-MCP-Toolsets"] == "secret_protection"
    assert headers["X-MCP-Features"] == "mcp_apps_disable_form_deferral"
    with pytest.raises(DiscoveryError):
        MCPConnection("github", "https://example.test/mcp/", True, github_catalogue=True)


def test_all_discovered_tools_exposed_without_twenty_tool_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = WebGitHubConnector({})
    schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def fake_exchange(code: str, verifier: str) -> tuple[str, str]:
        assert code and verifier
        return ("synthetic-fixture-value", "owner")

    async def fake_discover(connection: MCPConnection) -> tuple[ObservedTool, ...]:
        assert connection.endpoint == GITHUB_MCP_ENDPOINT
        assert connection.github_catalogue
        return tuple(observed(name, schema) for name in sorted(GITHUB_TOOLS)) + (
            observed("unexpected_tool", schema),
        )

    monkeypatch.setattr(connector, "_exchange", fake_exchange)
    monkeypatch.setattr("max_core.web.github_connect.discover_tools", fake_discover)
    state = "a" * 40
    connector._pending[state] = _Pending("fixture-verifier", time.monotonic())
    assert connector.finish(state, "fixture-code") == "owner"
    tools = connector.tools_for_session()
    assert {tool.name for tool in tools} == {"mcp.github." + name for name in GITHUB_TOOLS}
    assert all(tool.risk == ToolRisk.EFFECTFUL for tool in tools)
    assert connector.status()["unavailable_tools"] == []
    connector.disconnect()
    assert not connector.tools_for_session()


def test_structured_file_arguments_are_validated_before_approval() -> None:
    schema = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
            "title": {"type": "string"},
        },
        "required": ["files"],
    }
    entry = _reviewable(observed("push_files", schema))
    assert entry is not None
    connector = WebGitHubConnector({})
    connector._tools = (entry,)
    tool = connector.tools_for_session()[0]
    args = {"files": [{"path": "test.txt", "content": "example"}]}
    assert tool.validate(args) == args
    assert "title" in tool.describe()["arguments"]["properties"]
    for invalid in (
        {"files": [{"path": "test.txt"}]},
        {"files": [{"path": 3, "content": "example"}]},
        {"files": [], "unexpected": True},
        {"files": [{"path": "test.txt", "content": "x" * 9000}]},
    ):
        with pytest.raises(ToolError, match="github_invalid_arguments"):
            tool.validate(invalid)


def test_schema_references_and_digest_mismatch_fail_closed() -> None:
    ref = observed(
        "push_files",
        {"type": "object", "properties": {"files": {"$ref": "https://example.test/schema"}}},
    )
    assert _reviewable(ref) is None
    valid = observed("get_me", {"type": "object", "properties": {}})
    with pytest.raises(GitHubSchemaError):
        validate_github_arguments(valid.schema_json, "0" * 64, {})


def test_ui_get_requires_repository_except_for_org_issue_types() -> None:
    validate_github_operation("ui_get", {"method": "issue_types", "owner": "example"})
    validate_github_operation(
        "ui_get", {"method": "labels", "owner": "example", "repo": "project"}
    )
    for invalid in (
        {"method": "labels", "owner": "example"},
        {"method": "unsupported", "owner": "example", "repo": "project"},
        {"method": "issue_types", "owner": ""},
    ):
        with pytest.raises(GitHubSchemaError, match="github_invalid_ui_request"):
            validate_github_operation("ui_get", invalid)


def test_operation_checks_require_nonempty_content_or_reaction() -> None:
    validate_github_operation("run_secret_scanning", {"files": ["example source"]})
    validate_github_operation("run_secret_scanning", {"files": "example source"})
    validate_github_operation("add_issue_comment", {"reaction": "+1"})
    for invalid in ({"files": ""}, {"files": []}, {"files": [" "]}):
        with pytest.raises(GitHubSchemaError, match="github_scan_content_required"):
            validate_github_operation("run_secret_scanning", invalid)
    for name in ("add_issue_comment", "add_reply_to_pull_request_comment"):
        with pytest.raises(GitHubSchemaError, match="github_comment_or_reaction_required"):
            validate_github_operation(name, {"body": " "})
    with pytest.raises(GitHubSchemaError, match="github_file_batch_required"):
        validate_github_operation("push_files", {"files": []})
