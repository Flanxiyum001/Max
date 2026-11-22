"""Hosted GitHub OAuth flow and MCP tool-approval security regressions."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from max_core.mcp_discovery import ObservedTool
from max_core.tools import ToolError, ToolRisk
from max_core.web.github_connect import (
    GitHubConnectionError,
    WebGitHubConnector,
    github_connect_intent,
)
from max_core.web.app import create_app

CONFIG = {
    "MAX_WEB_PASSCODE": "correct horse battery",
    "MAX_WEB_INSECURE_COOKIE": "yes",
    "MAX_GITHUB_OAUTH_CLIENT_ID": "Ov23lixxxxxxxx123456",
    "MAX_GITHUB_OAUTH_CLIENT_SECRET": "test-secret-never-leaks",
    "MAX_GITHUB_OAUTH_CALLBACK_URL": "https://max.example.test/api/connect/github/callback",
}


def observation(*, nested: bool = False) -> ObservedTool:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "object" if nested else "string"},
        },
        "required": ["query"],
    }
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return ObservedTool(
        "github",
        "search",
        encoded,
        hashlib.sha256(encoded.encode()).hexdigest(),
        True,
    )


def authed(client: TestClient) -> None:
    assert client.post(
        "/api/login", json={"passcode": CONFIG["MAX_WEB_PASSCODE"]}
    ).status_code == 200


def test_owner_voice_command_routes_to_oauth_intent_without_model() -> None:
    assert github_connect_intent("Max, connect to GitHub") == "connect"
    assert github_connect_intent("MAX, please set up GitHub") == "connect"
    assert github_connect_intent("MAX disconnect GitHub") == "disconnect"
    assert github_connect_intent("How do I write a GitHub action?") is None
    app = create_app(CONFIG, session_factory=lambda: None)
    client = TestClient(app)
    assert client.post("/api/ask", json={"text": "MAX, connect to GitHub"}).status_code == 401
    authed(client)
    job_id = client.post("/api/ask", json={"text": "MAX connect to GitHub"}).json()["job"]
    result = client.get(f"/api/job/{job_id}").json()
    assert result["status"] == "done"
    assert result["connect"] == {"service": "github", "action": "authorize"}
    assert "Approve the GitHub authorization" in result["answer"]


def test_no_oauth_app_configuration_never_fabricates_working_connection() -> None:
    client = TestClient(create_app(
        {"MAX_WEB_PASSCODE": CONFIG["MAX_WEB_PASSCODE"], "MAX_WEB_INSECURE_COOKIE": "yes"},
        session_factory=lambda: None,
    ))
    authed(client)
    job = client.post("/api/ask", json={"text": "connect to GitHub"}).json()["job"]
    assert client.get(f"/api/job/{job}").json()["connect"]["action"] == "needs_configuration"
    assert client.post("/api/connect/github/start").status_code == 503
    assert client.get("/api/state").json()["github"]["configured"] is False


def test_server_only_oauth_state_pkce_and_callback_validation() -> None:
    connector = WebGitHubConnector(CONFIG)
    url = connector.start()
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https", "github.com", "/login/oauth/authorize"
    )
    query = parse_qs(parsed.query)
    assert query["redirect_uri"] == [CONFIG["MAX_GITHUB_OAUTH_CALLBACK_URL"]]
    assert query["scope"] == ["repo read:user"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert "test-secret" not in url
    assert "code_verifier" not in url
    with pytest.raises(GitHubConnectionError, match="github_oauth_state_invalid"):
        connector.finish("forged-state", "code")


def test_real_callback_consumes_one_use_state_and_discovers_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = WebGitHubConnector(CONFIG)
    app = create_app(CONFIG, github_connector=connector, session_factory=lambda: None)
    client = TestClient(app, follow_redirects=False)
    authed(client)

    async def fake_exchange(_code: str, verifier: str) -> tuple[str, str]:
        assert verifier
        return ("ghu_token_that_must_never_be_in_responses", "Flanxiyum001")

    async def fake_discover(connection: object) -> tuple[ObservedTool, ...]:
        assert getattr(connection, "bearer_token") == "ghu_token_that_must_never_be_in_responses"
        return (observation(), observation(nested=True))

    monkeypatch.setattr(connector, "_exchange", fake_exchange)
    monkeypatch.setattr("max_core.web.github_connect.discover_tools", fake_discover)
    started = client.post("/api/connect/github/start")
    assert started.status_code == 200
    start_url = started.json()["url"]
    state = parse_qs(urlsplit(start_url).query)["state"][0]
    callback = client.get(
        "/api/connect/github/callback", params={"state": state, "code": "owner-approved"}
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?github=connected"
    assert "ghu_token" not in str(callback.headers)
    status = client.get("/api/state").json()["github"]
    assert status["connected"] is True
    assert status["account"] == "Flanxiyum001"
    assert status["tools"] == 1
    assert "ghu_token" not in str(status)
    assert len(connector.tools_for_session()) == 1
    assert connector.tools_for_session()[0].risk == ToolRisk.EFFECTFUL
    assert client.get(
        "/api/connect/github/callback", params={"state": state, "code": "replay"}
    ).headers["location"] == "/?github=failed"

    disconnected = client.post("/api/connect/github/disconnect")
    assert disconnected.status_code == 200
    assert client.get("/api/state").json()["github"]["connected"] is False


def test_tool_revoked_during_pending_approval_cannot_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = WebGitHubConnector(CONFIG)
    with connector._lock:
        connector._token = "test_token_that_is_not_exported"
        connector._account = "owner"
        from max_core.web.github_connect import _reviewable

        entry = _reviewable(observation())
        assert entry is not None
        connector._tools = (entry,)
        connector._generation = 5
    tool = connector.tools_for_session()[0]
    connector.disconnect()
    with pytest.raises(ToolError, match="github_mcp_revoked"):
        tool.handler({"query": "secret"})


def test_bad_callback_and_public_route_do_not_expose_connection() -> None:
    client = TestClient(create_app(CONFIG, session_factory=lambda: None), follow_redirects=False)
    assert client.post("/api/connect/github/start").status_code == 401
    assert client.post("/api/connect/github/disconnect").status_code == 401
    assert client.get(
        "/api/connect/github/callback", params={"state": "bad", "code": "test"}
    ).headers["location"] == "/?github=failed"
    authed(client)
    assert client.get("/api/state").json()["github"]["connected"] is False
