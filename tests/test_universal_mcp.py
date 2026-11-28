from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from max_core.assistant_session import AssistantSession, SessionOptions
from max_core.gateway import GatewayResponse
from max_core.gateway_trial import TrialSettings
from max_core.mcp_discovery import ObservedTool
from max_core.mcp_execute import MCPToolExecutor
from max_core.tools import ToolError
from max_core.web.mcp_hub import WebMCPHub


def _fake_settings() -> TrialSettings:
    return TrialSettings.from_env(
        {
            "MAX_TRIAL_DESTINATION": "verified_local",
            "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
            "MAX_TRIAL_MODEL": "test-model",
            "MAX_TRIAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
            "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
            "MAX_TRIAL_CONTEXT_TOKENS": "8192",
            "MAX_TRIAL_API_KEY": "test-key",
        }
    )


class _StubMCPCore:
    def __init__(self, definitions: tuple[SimpleNamespace, ...]) -> None:
        self._definitions = definitions
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._mcp_executor = self._make_executor()

    def _make_executor(self) -> MCPToolExecutor:
        executor = object.__new__(MCPToolExecutor)
        executor.tool_definitions = lambda: self._definitions  # type: ignore[method-assign]
        return executor

    @property
    def tool_executor(self) -> Any:
        return self._mcp_executor


def test_assistant_session_preserves_both_desktop_and_hub_mcp_tools(tmp_path: Path) -> None:
    core = _StubMCPCore(
        (
            SimpleNamespace(
                name="mcp.playwright.browser_snapshot",
                description="Take accessibility snapshot",
                parameters={"type": "object", "properties": {}},
            ),
            SimpleNamespace(
                name="mcp.Custom-Server.createIssue",
                description="Create issue with tags",
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "labels": {"type": "array"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title", "labels"],
                },
            ),
        )
    )
    hub = WebMCPHub({})
    session = AssistantSession(
        _fake_settings(),
        SessionOptions(
            data_dir=tmp_path,
            workspace=tmp_path,
            cloud_personal_tools=False,
            enable_shell=False,
            enable_web=False,
        ),
        mcp_core=core,  # type: ignore[arg-type]
        github_connector=hub,
    )
    try:
        names = {t.name for t in session.registry}
        assert "mcp.playwright.browser_snapshot" in names
        assert "mcp.custom_server.createissue" in names
        assert "mcp.list" in names
        assert "mcp.connect" in names
        assert "mcp.disconnect" in names

        # Calling _refresh_all_mcp_tools preserves both sources without clobbering.
        session._refresh_all_mcp_tools()
        names_after = {t.name for t in session.registry}
        assert "mcp.playwright.browser_snapshot" in names_after
        assert "mcp.list" in names_after

        # Complex array/object schema accepts both JSON strings and native containers.
        complex_tool = session.registry.get("mcp.custom_server.createissue")
        assert complex_tool is not None
        cleaned = complex_tool.validate(
            {
                "title": "Bug",
                "labels": '["urgent", "mcp"]',
                "metadata": '{"priority": 1}',
            }
        )
        assert cleaned == {
            "title": "Bug",
            "labels": ["urgent", "mcp"],
            "metadata": {"priority": 1},
        }
    finally:
        session.close()


def test_mcp_connect_and_dynamic_tool_call_in_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = json.dumps(
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    observed = (
        ObservedTool(
            connection_id="acme",
            name="create_issue",
            schema_json=schema,
            schema_sha256=hashlib.sha256(schema.encode()).hexdigest(),
            read_only_hint=False,
        ),
    )

    async def _fake_discover(_conn: Any) -> tuple[ObservedTool, ...]:
        return observed

    dispatched: list[tuple[str, dict[str, Any]]] = []

    async def _fake_dispatch(
        self: WebMCPHub,
        service: str,
        token: str,
        entry: Any,
        args: dict[str, Any],
        generation: int,
    ) -> str:
        dispatched.append((f"{service}.{entry.observation.name}", args))
        return json.dumps({"ok": True, "id": "LIN-42"})

    monkeypatch.setattr("max_core.web.mcp_hub.discover_tools", _fake_discover)
    monkeypatch.setattr(WebMCPHub, "_dispatch", _fake_dispatch)

    hub = WebMCPHub({})
    session = AssistantSession(
        _fake_settings(),
        SessionOptions(
            data_dir=tmp_path,
            workspace=tmp_path,
            cloud_personal_tools=False,
            enable_shell=False,
            enable_web=False,
        ),
        github_connector=hub,
    )
    session._magnitude = True  # exercise ToolLoop directly with mocked Gateway.complete

    prompts_seen: list[str] = []
    call_count = 0

    async def _fake_complete(self: Any, request: Any) -> GatewayResponse:
        nonlocal call_count
        call_count += 1
        prompts_seen.append(
            "\n".join(m.content for m in request.messages if m.role == "system")
        )
        if call_count == 1:
            return GatewayResponse(
                request_id=request.request_id,
                provider_id="owner-trial",
                model_id="test-model",
                content='<tool_call>{"name": "mcp.connect", "arguments": {"alias": "acme", "url": "https://mcp.acme.example/mcp"}}</tool_call>',
                tool_proposals=(),
                finish_reason="stop",
                usage=None,
                estimated_cost_usd=None,
            )
        if call_count == 2:
            return GatewayResponse(
                request_id=request.request_id,
                provider_id="owner-trial",
                model_id="test-model",
                content='<tool_call>{"name": "mcp.acme.create_issue", "arguments": {"title": "Fix MCP"}}</tool_call>',
                tool_proposals=(),
                finish_reason="stop",
                usage=None,
                estimated_cost_usd=None,
            )
        return GatewayResponse(
            request_id=request.request_id,
            provider_id="owner-trial",
            model_id="test-model",
            content="Connected Acme and created issue LIN-42.",
            tool_proposals=(),
            finish_reason="stop",
            usage=None,
            estimated_cost_usd=None,
        )

    try:
        with patch("max_core.gateway.Gateway.complete", new=_fake_complete):
            # Denied when approve returns False: mcp.connect must not execute.
            denied = session.ask(
                "Connect Acme MCP and create an issue",
                approve=lambda _name, _args: False,
            )
            assert any(s.tool_name == "mcp.connect" and s.status == "denied" for s in denied.steps)
            assert dispatched == []

            # Reset turn counter for approved run.
            call_count = 0
            prompts_seen.clear()
            approved = session.ask(
                "Connect Acme MCP and create an issue",
                approve=lambda _name, _args: True,
            )
        assert "LIN-42" in approved.answer
        assert dispatched == [("acme.create_issue", {"title": "Fix MCP"})]
        # Step 2 system prompt must include the newly connected mcp.acme.create_issue tool!
        assert len(prompts_seen) == 3
        assert "mcp.acme.create_issue" not in prompts_seen[0]
        assert "mcp.acme.create_issue" in prompts_seen[1]

        # mcp.list reflects the connected server, and mcp.disconnect removes it.
        listed = json.loads(hub._list_connected({}))
        assert "acme" in listed["services"]
        hub.disconnect("acme")
        listed_after = json.loads(hub._list_connected({}))
        assert "acme" not in listed_after["services"]
    finally:
        session.close()


def test_mcp_connect_rejects_non_https_and_reserved_aliases() -> None:
    hub = WebMCPHub({})
    with pytest.raises(ToolError, match="invalid_mcp_endpoint"):
        hub.connect_custom("myserver", "http://insecure.example/mcp")
    with pytest.raises(ToolError, match="invalid_mcp_alias"):
        hub.connect_custom("github", "https://example.com/mcp")
    with pytest.raises(ToolError, match="invalid_mcp_alias"):
        hub.connect_custom("notion", "https://example.com/mcp")
