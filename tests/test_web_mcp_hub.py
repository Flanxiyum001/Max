"""Generic MCP hub: provider catalog, OAuth DCR/CIMD, vault and revocation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from max_core.mcp_discovery import MCPConnection, ObservedTool
from max_core.tools import ToolError, ToolRisk
from max_core.web.app import create_app
from max_core.web.mcp_hub import (
    CATALOG,
    MCPHubError,
    WebMCPHub,
    _trusted_url,
    connection_intent,
    public_base_url,
)
from max_core.web.mcp_vault import MCPCredentialVault

BASE = {
    "MAX_WEB_PASSCODE": "long enough web passcode",
    "MAX_WEB_INSECURE_COOKIE": "yes",
    "RENDER_EXTERNAL_URL": "https://max-test.onrender.com",
    "MAX_WEB_SECRET": "one_long_stable_application_secret_for_all_apps",
}


class MemoryVault:
    available = False
    durable = False

    def load(self, service: str) -> None:
        return None

    def save(self, service: str, data: dict[str, object]) -> None:
        return None

    def delete(self, service: str) -> None:
        return None


def _observed(service: str, name: str = "search") -> ObservedTool:
    schema = json.dumps(
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ObservedTool(service, name, schema, hashlib.sha256(schema.encode()).hexdigest(), True)


def test_intent_routing_is_curated() -> None:
    assert connection_intent("MAX connect to Cloudflare") == ("connect", "cloudflare")
    assert connection_intent("Max, link Railway") == ("connect", "railway")
    assert connection_intent("Disconnect Notion") == ("disconnect", "notion")
    assert connection_intent("Summarize my Railway project") is None
    assert connection_intent("connect to an unknown MCP URL") is None
    assert "cloudflare" in CATALOG and "railway" in CATALOG


def test_trusted_provider_endpoint_and_render_origin() -> None:
    assert _trusted_url("https://accounts.cloudflare.com/oauth", CATALOG["cloudflare"])
    assert not _trusted_url("https://cloudflare.com.evil.invalid/oauth", CATALOG["cloudflare"])
    assert not _trusted_url("http://cloudflare.com/oauth", CATALOG["cloudflare"])
    assert not _trusted_url("https://user:pass@cloudflare.com/oauth", CATALOG["cloudflare"])
    assert not _trusted_url("https://127.0.0.1/oauth", CATALOG["cloudflare"])
    assert public_base_url(BASE) == "https://max-test.onrender.com"
    assert public_base_url({"RENDER_EXTERNAL_URL": "http://localhost:8000"}) is None


def test_owner_can_start_any_catalog_service_without_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebMCPHub(BASE, vault=MemoryVault())
    assert hub.configured
    assert hub.status()["services"]["cloudflare"]["connected"] is False

    async def metadata(_provider: object) -> tuple[str, dict[str, object]]:
        return "https://accounts.cloudflare.com", {
            "authorization_endpoint": "https://accounts.cloudflare.com/oauth/authorize",
            "token_endpoint": "https://accounts.cloudflare.com/oauth/token",
            "client_id_metadata_document_supported": True,
        }

    async def register(_service: str, _issuer: str, _meta: dict[str, object]) -> str:
        return "https://max-test.onrender.com/.well-known/oauth-client-metadata/max.json"

    monkeypatch.setattr(hub, "_oauth_metadata", metadata)
    monkeypatch.setattr(hub, "_register_client", register)
    url = hub.start("cloudflare")
    params = parse_qs(urlsplit(url).query)
    assert params["resource"] == ["https://mcp.cloudflare.com/mcp"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == ["https://max-test.onrender.com/api/connect/mcp/callback"]
    assert "MAX_CLOUDFLARE_TOKEN" not in str(params)
    assert len(params["state"][0]) > 32
    assert hub.client_metadata()["client_id"] == params["client_id"][0]


def test_server_side_oauth_callback_discovers_and_revokes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebMCPHub(BASE, vault=MemoryVault())

    async def metadata(_provider: object) -> tuple[str, dict[str, object]]:
        return "https://railway.com", {
            "authorization_endpoint": "https://railway.com/oauth/authorize",
            "token_endpoint": "https://railway.com/oauth/token",
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }

    async def register(*_args: object) -> str:
        return "https://max-test.onrender.com/.well-known/oauth-client-metadata/max.json"

    async def token(_provider: object, _endpoint: str, payload: dict[str, str]) -> dict[str, str]:
        assert payload["resource"] == "https://mcp.railway.com"
        return {
            "access_token": "fake_secret_provider_token_12345",
            "refresh_token": "fake_refresh_provider_token_12345",
        }

    async def discovery(connection: MCPConnection) -> tuple[ObservedTool, ...]:
        assert connection.endpoint == "https://mcp.railway.com"
        assert connection.bearer_token == "fake_secret_provider_token_12345"
        return (_observed("railway"),)

    monkeypatch.setattr(hub, "_oauth_metadata", metadata)
    monkeypatch.setattr(hub, "_register_client", register)
    monkeypatch.setattr(hub, "_token_request", token)
    monkeypatch.setattr("max_core.web.mcp_hub.discover_tools", discovery)
    state = parse_qs(urlsplit(hub.start("railway")).query)["state"][0]
    with pytest.raises(MCPHubError, match="mcp_oauth_issuer_mismatch"):
        hub.finish(state, "code", "https://evil.invalid")
    state = parse_qs(urlsplit(hub.start("railway")).query)["state"][0]
    assert hub.finish(state, "code", "https://railway.com") == "railway"
    assert hub.status()["services"]["railway"]["connected"]
    tools = hub.tools_for_session()
    railway_tools = [t for t in tools if t.name.startswith("mcp.railway.")]
    assert len(railway_tools) == 1
    assert railway_tools[0].name == "mcp.railway.search"
    assert railway_tools[0].risk == ToolRisk.EFFECTFUL
    hub.disconnect("railway")
    assert [t for t in hub.tools_for_session() if t.name.startswith("mcp.railway.")] == []
    with pytest.raises(ToolError, match="mcp_connection_revoked"):
        railway_tools[0].handler({"query": "secret"})


def test_web_api_client_metadata_and_voice_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebMCPHub(BASE, vault=MemoryVault())
    client = TestClient(create_app(BASE, mcp_hub=hub, session_factory=lambda: None))
    assert client.get("/.well-known/oauth-client-metadata/max.json").status_code == 200
    assert client.post("/api/connect/mcp/railway/start", json={}).status_code == 401
    assert client.post("/api/login", json={"passcode": BASE["MAX_WEB_PASSCODE"]}).status_code == 200
    job = client.post("/api/ask", json={"text": "MAX connect to Railway"}).json()["job"]
    done = client.get(f"/api/job/{job}").json()
    assert done["connect"] == {"service": "railway", "action": "authorize"}
    assert "Railway" in done["answer"]
    assert (
        client.get("/api/state").json()["connections"]["services"]["railway"]["connected"] is False
    )
    assert client.post("/api/connect/mcp/not-made-up/disconnect", json={}).status_code == 404


def test_encrypted_single_vault_persists_without_provider_secrets_in_environment(
    tmp_path: Path,
) -> None:
    env = {**BASE, "MAX_DATA_DIR": str(tmp_path)}
    first = MCPCredentialVault(env)
    assert first.available
    first.save("railway", {"token": "super-secret-railway-access-token"})
    raw = (tmp_path / "mcp-vault.sqlite3").read_bytes()
    assert b"super-secret-railway-access-token" not in raw
    assert b"MAX_RAILWAY_API_KEY" not in raw
    loaded = MCPCredentialVault(env).load("railway")
    assert loaded == {"token": "super-secret-railway-access-token"}
    different = MCPCredentialVault(
        {**env, "MAX_WEB_SECRET": "different_application_secret_123456789"}
    )
    assert different.load("railway") is None
    first.delete("railway")
    assert first.load("railway") is None


def test_oauth_resource_discovery_and_dynamic_client_registration() -> None:
    """A provider supporting DCR needs NO per-app client ID or secret in Render."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.cloudflare.com/mcp",
                    "authorization_servers": ["https://accounts.cloudflare.com"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://accounts.cloudflare.com",
                    "authorization_endpoint": "https://accounts.cloudflare.com/oauth/authorize",
                    "token_endpoint": "https://accounts.cloudflare.com/oauth/token",
                    "registration_endpoint": "https://accounts.cloudflare.com/oauth/register",
                },
            )
        if request.url.path == "/oauth/register":
            assert request.method == "POST"
            registration = json.loads(request.content)
            assert registration["redirect_uris"] == [
                "https://max-test.onrender.com/api/connect/mcp/callback"
            ]
            assert registration["token_endpoint_auth_method"] == "none"
            return httpx.Response(
                201,
                json={
                    "client_id": "dcr-issued-max-public-client",
                    "token_endpoint_auth_method": "none",
                },
            )
        return httpx.Response(404)

    hub = WebMCPHub(BASE, vault=MemoryVault(), oauth_transport=httpx.MockTransport(handler))
    url = hub.start("cloudflare")
    assert url.startswith("https://accounts.cloudflare.com/oauth/authorize?")
    args = parse_qs(urlsplit(url).query)
    assert args["client_id"] == ["dcr-issued-max-public-client"]
    assert args["code_challenge_method"] == ["S256"]
    assert len(requests) == 3


def test_provider_metadata_cannot_redirect_oauth_to_an_untrusted_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "authorization_servers": ["https://evil.example.invalid"],
                },
            )
        return httpx.Response(404)

    hub = WebMCPHub(BASE, vault=MemoryVault(), oauth_transport=httpx.MockTransport(handler))
    with pytest.raises(MCPHubError, match="mcp_oauth_issuer_not_trusted"):
        hub.start("cloudflare")


# --- API-key services (Render) and the orb-only UI ------------------------------------


def _key_hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WebMCPHub:
    seen: list[MCPConnection] = []

    async def discovery(connection: MCPConnection) -> tuple[ObservedTool, ...]:
        seen.append(connection)
        return (_observed("render", "list_services"),)

    monkeypatch.setattr("max_core.web.mcp_hub.discover_tools", discovery)
    hub = WebMCPHub(BASE, vault=MemoryVault())  # type: ignore[arg-type]
    hub.seen = seen  # type: ignore[attr-defined]
    return hub


def test_render_connects_with_api_key_not_oauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hub = _key_hub(monkeypatch, tmp_path)
    assert connection_intent("MAX, connect to Render") == ("connect", "render")
    with pytest.raises(MCPHubError, match="mcp_needs_api_key"):
        hub.start("render")
    with pytest.raises(MCPHubError, match="mcp_api_key_invalid"):
        hub.connect_with_key("render", "not-a-render-key")
    assert hub.connect_with_key("render", "rnd_" + "A" * 24) == 1
    assert hub.seen[0].endpoint == "https://mcp.render.com/mcp"  # type: ignore[attr-defined]
    assert hub.status()["services"]["render"]["connected"]
    tools = {t.name: t for t in hub.tools_for_session()}
    assert tools["mcp.render.list_services"].risk == ToolRisk.EFFECTFUL


def test_voice_connect_render_asks_for_key_and_route_stores_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hub = _key_hub(monkeypatch, tmp_path)
    client = TestClient(create_app(BASE, mcp_hub=hub))
    assert client.post("/api/login", json={"passcode": BASE["MAX_WEB_PASSCODE"]}).status_code == 200
    job = client.post("/api/ask", json={"text": "MAX connect to Render"}).json()["job"]
    data = client.get(f"/api/job/{job}").json()
    assert data["connect"] == {
        "service": "render", "action": "api_key",
        "url": "https://dashboard.render.com/u/settings#api-keys",
    }  # fmt: skip
    assert client.post("/api/connect/mcp/render/key", json={"key": "bad key!"}).status_code == 400
    ok = client.post("/api/connect/mcp/render/key", json={"key": "rnd_" + "b" * 30})
    assert ok.json() == {"ok": True, "tools": 1}
    job = client.post("/api/ask", json={"text": "which apps are connected?"}).json()["job"]
    answer = client.get(f"/api/job/{job}").json()["answer"]
    assert answer.startswith("Connected: Render.") and "Cloudflare" in answer


def test_key_route_requires_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = TestClient(create_app(BASE, mcp_hub=_key_hub(monkeypatch, tmp_path)))
    res = client.post("/api/connect/mcp/render/key", json={"key": "rnd_" + "b" * 30})
    assert res.status_code == 401
