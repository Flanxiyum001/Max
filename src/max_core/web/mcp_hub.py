"""MAX-owned multi-app MCP Connection Hub for the hosted orb.

Owner says "connect to Cloudflare" / "connect to Railway", MAX discovers
the provider's OAuth metadata, uses CIMD or dynamic registration, and sends
the owner to the real authorization screen. No per-app tokens/URLs/env vars.

Curated official endpoints only. Arbitrary model-provided URLs are NOT trusted
network destinations. GitHub remains a pre-registered OAuth exception.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from max_core.mcp_discovery import MCPConnection, ObservedTool, discover_tools
from max_core.mcp_execute import _extract_tool_output, _run_in_fresh_thread
from max_core.mcp_policy import validated_arguments
from max_core.mcp_transport import EndpointTransport
from max_core.tools import Tool, ToolError, ToolParam, ToolRisk
from max_core.web.github_connect import (
    GitHubConnectionError,
    WebGitHubConnector,
    _RemoteTool,
    _reviewable,
)
from max_core.web.mcp_vault import MCPCredentialVault


# Official vendor endpoints, not supplied by a prompt or remote MCP response.
@dataclass(frozen=True)
class MCPProvider:
    name: str
    endpoint: str
    trusted_domains: tuple[str, ...]


CATALOG: dict[str, MCPProvider] = {
    "cloudflare": MCPProvider("Cloudflare", "https://mcp.cloudflare.com/mcp", ("cloudflare.com",)),
    "railway": MCPProvider("Railway", "https://mcp.railway.com", ("railway.com",)),
    "notion": MCPProvider("Notion", "https://mcp.notion.com/mcp", ("notion.com", "makenotion.com")),
    "linear": MCPProvider("Linear", "https://mcp.linear.app/mcp", ("linear.app",)),
    "sentry": MCPProvider("Sentry", "https://mcp.sentry.dev/mcp", ("sentry.dev", "sentry.io")),
    "figma": MCPProvider("Figma", "https://mcp.figma.com/mcp", ("figma.com",)),
    "render": MCPProvider("Render", "https://mcp.render.com/mcp", ("render.com",)),
}
# Services whose official MCP server has no OAuth: the owner creates an API key on
# the provider's own dashboard and hands it to MAX once (stored encrypted).
API_KEY_SERVICES: dict[str, tuple[str, re.Pattern[str]]] = {
    "render": (
        "https://dashboard.render.com/u/settings#api-keys",
        re.compile(r"rnd_[A-Za-z0-9]{16,128}\Z"),
    ),
}
_SERVICE = re.compile(r"\b(github|cloudflare|railway|notion|linear|sentry|figma|render)\b", re.I)
_CONNECT = re.compile(r"\b(connect|link|integrate|set\s*up|setup|sign\s*in|log\s*in)\b", re.I)
_DISCONNECT = re.compile(r"\b(disconnect|unlink|revoke)\b", re.I)
_OAUTH_STATE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_TOKEN_BYTES = 16_384
_PENDING_SECONDS = 600
_MAX_TOOLS = 20


class MCPHubError(ValueError):
    """Fixed safe error code; never includes any provider URL response or token."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def connection_intent(text: str) -> tuple[str, str] | None:
    """Only owner-authenticated user text is allowed to trigger connection setup."""
    m = _SERVICE.search(text[:180])
    if not m:
        return None
    if _DISCONNECT.search(text[:180]):
        return "disconnect", m.group(1).lower()
    if _CONNECT.search(text[:180]):
        return "connect", m.group(1).lower()
    return None


def public_base_url(env: Mapping[str, str]) -> str | None:
    """Render supplies its public URL without any owner-entered app variable."""
    url = (env.get("MAX_MCP_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or len(url) > 512
        ):
            return None
    except ValueError:
        return None
    return url


def _trusted_url(url: str, provider: MCPProvider) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and len(url) <= 2048
            and any(
                host == domain or host.endswith("." + domain) for domain in provider.trusted_domains
            )
        )
    except ValueError:
        return False


def _metadata_url(endpoint: str, segment: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/{segment}{path}"


@dataclass(frozen=True)
class _Pending:
    service: str
    verifier: str
    created: float
    issuer: str
    client_id: str
    token_endpoint: str
    # registration-specific secret is NEVER required; only public PKCE clients.
    auth_issuer_parameter: bool


@dataclass(frozen=True)
class _Connection:
    token: str
    refresh_token: str | None
    expires_at: float | None
    issuer: str
    client_id: str
    token_endpoint: str
    tools: tuple[_RemoteTool, ...]
    generation: int


class WebMCPHub:
    """Curated services + GitHub exception, with one shared encrypted vault."""

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        github_connector: WebGitHubConnector | None = None,
        vault: MCPCredentialVault | None = None,
        oauth_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._env = dict(env)
        self._oauth_transport = oauth_transport
        self._github = github_connector or WebGitHubConnector(env)
        self._base = public_base_url(env)
        self._vault = vault or MCPCredentialVault(env)
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._connections: dict[str, _Connection] = {}
        self._custom_endpoints: dict[str, str] = {}
        self._generations: dict[str, int] = {}
        self._load_connections()

    @property
    def github(self) -> WebGitHubConnector:
        return self._github

    @property
    def configured(self) -> bool:
        return self._base is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "services": {
                    "github": self._github.status(),
                    **{
                        service: {
                            "configured": self._base is not None,
                            "connected": service in self._connections,
                            "tools": len(self._connections[service].tools)
                            if service in self._connections
                            else 0,
                            "ephemeral": not self._vault.durable,
                        }
                        for service in CATALOG
                    },
                },
                "catalog": ("github", *CATALOG),
                "vault_persistent": self._vault.durable,
            }

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, provider: MCPProvider
    ) -> dict[str, Any] | None:
        if not _trusted_url(url, provider):
            return None
        try:
            response = await client.get(
                url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            )
            if response.status_code != 200 or len(response.content) > _TOKEN_BYTES:
                return None
            obj = response.json()
            return obj if isinstance(obj, dict) else None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _oauth_metadata(self, provider: MCPProvider) -> tuple[str, dict[str, Any]]:
        async with httpx.AsyncClient(
            transport=self._oauth_transport, timeout=10.0, follow_redirects=False, trust_env=False
        ) as client:
            resource_urls = [
                _metadata_url(provider.endpoint, "oauth-protected-resource"),
                provider.endpoint.rstrip("/") + "/.well-known/oauth-protected-resource",
                urlunsplit(
                    (
                        urlsplit(provider.endpoint).scheme,
                        urlsplit(provider.endpoint).netloc,
                        "/.well-known/oauth-protected-resource",
                        "",
                        "",
                    )
                ),
            ]
            resource = None
            for uri in resource_urls:
                resource = await self._get_json(client, uri, provider)
                if resource is not None:
                    break
            if resource is None:
                raise MCPHubError("mcp_oauth_resource_metadata_missing")
            servers = resource.get("authorization_servers", [])
            if (
                not isinstance(servers, list)
                or not servers
                or not isinstance(servers[0], str)
                or not _trusted_url(servers[0], provider)
            ):
                raise MCPHubError("mcp_oauth_issuer_not_trusted")
            issuer = servers[0]
            parsed = urlsplit(issuer)
            root = f"{parsed.scheme}://{parsed.netloc}"
            issuer_path = parsed.path.rstrip("/")
            candidates = [
                f"{root}/.well-known/oauth-authorization-server{issuer_path}",
                f"{root}/.well-known/openid-configuration{issuer_path}",
            ]
            for uri in candidates:
                data = await self._get_json(client, uri, provider)
                if data is not None and data.get("issuer") == issuer:
                    auth_url = data.get("authorization_endpoint")
                    token_url = data.get("token_endpoint")
                    if (
                        isinstance(auth_url, str)
                        and isinstance(token_url, str)
                        and _trusted_url(auth_url, provider)
                        and _trusted_url(token_url, provider)
                    ):
                        return issuer, data
        raise MCPHubError("mcp_oauth_authorization_metadata_missing")

    async def _register_client(self, service: str, issuer: str, meta: dict[str, Any]) -> str:
        assert self._base is not None
        callback = self._base + "/api/connect/mcp/callback"
        metadata_id = self._base + "/.well-known/oauth-client-metadata/max.json"
        if meta.get("client_id_metadata_document_supported") is True:
            return metadata_id
        endpoint = meta.get("registration_endpoint")
        provider = CATALOG[service]
        if not isinstance(endpoint, str) or not _trusted_url(endpoint, provider):
            raise MCPHubError("mcp_oauth_registration_not_supported")
        body = {
            "client_name": "MAX Personal Assistant",
            "redirect_uris": [callback],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_uri": self._base,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._oauth_transport,
                timeout=12.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    endpoint,
                    json=body,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
                if response.status_code not in (200, 201) or len(response.content) > _TOKEN_BYTES:
                    raise MCPHubError("mcp_oauth_registration_failed")
                data = response.json()
        except MCPHubError:
            raise
        except (ValueError, httpx.HTTPError):
            raise MCPHubError("mcp_oauth_registration_failed") from None
        client_id = data.get("client_id") if isinstance(data, dict) else None
        auth_method = data.get("token_endpoint_auth_method", "none")
        if (
            not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 1024
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in client_id)
            or auth_method != "none"
        ):
            raise MCPHubError("mcp_oauth_registration_failed")
        return client_id

    async def _start_auto(self, service: str) -> str:
        if self._base is None:
            raise MCPHubError("mcp_public_url_missing")
        provider = CATALOG[service]
        issuer, metadata = await self._oauth_metadata(provider)
        client_id = await self._register_client(service, issuer, metadata)
        state = secrets.token_urlsafe(36)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        with self._lock:
            self._pending = {
                key: item
                for key, item in self._pending.items()
                if time.monotonic() - item.created < _PENDING_SECONDS
            }
            self._pending[state] = _Pending(
                service,
                verifier,
                time.monotonic(),
                issuer,
                client_id,
                metadata["token_endpoint"],
                metadata.get("authorization_response_iss_parameter_supported") is True,
            )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._base + "/api/connect/mcp/callback",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": provider.endpoint,
        }
        scopes = metadata.get("scopes_supported")
        # Never request all declared scopes automatically.
        if isinstance(scopes, list) and "openid" in scopes:
            params["scope"] = "openid"
        authorization_endpoint = metadata.get("authorization_endpoint")
        if not isinstance(authorization_endpoint, str) or not _trusted_url(
            authorization_endpoint, provider
        ):
            raise MCPHubError("mcp_oauth_authorization_endpoint_invalid")
        return authorization_endpoint + "?" + urlencode(params)

    def start(self, service: str) -> str:
        if service == "github":
            try:
                return self._github.start()
            except GitHubConnectionError as exc:
                raise MCPHubError(exc.code) from None
        if service not in CATALOG:
            raise MCPHubError("unknown_mcp_service")
        if service in API_KEY_SERVICES:
            raise MCPHubError("mcp_needs_api_key")
        try:
            return asyncio.run(self._start_auto(service))
        except MCPHubError:
            raise
        except Exception:
            raise MCPHubError("mcp_oauth_start_failed") from None

    def client_metadata(self) -> dict[str, Any]:
        if self._base is None:
            raise MCPHubError("mcp_public_url_missing")
        return {
            "client_id": self._base + "/.well-known/oauth-client-metadata/max.json",
            "client_name": "MAX Personal Assistant",
            "client_uri": self._base,
            "redirect_uris": [self._base + "/api/connect/mcp/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    async def _token_request(
        self, provider: MCPProvider, endpoint: str, payload: dict[str, str]
    ) -> dict[str, Any]:
        if not _trusted_url(endpoint, provider):
            raise MCPHubError("mcp_oauth_token_endpoint_untrusted")
        try:
            async with httpx.AsyncClient(
                transport=self._oauth_transport,
                timeout=12.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    endpoint,
                    data=payload,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
                if response.status_code != 200 or len(response.content) > _TOKEN_BYTES:
                    raise MCPHubError("mcp_oauth_token_exchange_failed")
                data = response.json()
                if not isinstance(data, dict):
                    raise MCPHubError("mcp_oauth_token_exchange_failed")
                return data
        except MCPHubError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise MCPHubError("mcp_oauth_token_exchange_failed") from None

    @staticmethod
    def _parse_token(data: Mapping[str, Any]) -> tuple[str, str | None, float | None]:
        token = data.get("access_token")
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        if (
            not isinstance(token, str)
            or not 12 <= len(token) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or (
                refresh is not None
                and (
                    not isinstance(refresh, str)
                    or len(refresh) > 8192
                    or any(ord(c) < 33 or ord(c) > 126 for c in refresh)
                )
            )
        ):
            raise MCPHubError("mcp_oauth_token_invalid")
        deadline = None
        if isinstance(expires, (int, float)) and 0 < expires < 31536000:
            deadline = time.time() + float(expires)
        return token, refresh, deadline

    def connect_with_key(self, service: str, key: str) -> int:
        """API-key services (no OAuth): verify the key by discovering tools, then store it."""
        spec = API_KEY_SERVICES.get(service)
        key = key.strip()
        if spec is None:
            raise MCPHubError("unknown_mcp_service")
        if not spec[1].fullmatch(key):
            raise MCPHubError("mcp_api_key_invalid")
        provider = CATALOG[service]
        try:
            observed = asyncio.run(
                discover_tools(MCPConnection(service, provider.endpoint, True, key))
            )
        except Exception:
            raise MCPHubError("mcp_discovery_failed") from None
        reviewed = tuple(
            sorted(
                (e for t in observed if (e := _reviewable(t, service=service)) is not None),
                key=lambda e: (not e.observation.read_only_hint, e.observation.name),
            )
        )[:_MAX_TOOLS]
        if not reviewed:
            raise MCPHubError("mcp_no_compatible_tools")
        with self._lock:
            generation = self._generations.get(service, 0) + 1
            self._generations[service] = generation
            record = _Connection(
                key,
                None,
                None,
                provider.endpoint,
                "api_key",
                provider.endpoint,
                reviewed,
                generation,
            )
            self._connections[service] = record
            self._save(service, record)
        return len(reviewed)

    def finish(self, state: str, code: str, iss: str = "") -> str:
        if not _OAUTH_STATE.fullmatch(state) or not 1 <= len(code) <= 2048:
            raise MCPHubError("mcp_oauth_state_invalid")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.monotonic() - pending.created > _PENDING_SECONDS:
            raise MCPHubError("mcp_oauth_state_invalid")
        if (iss and iss != pending.issuer) or (
            pending.auth_issuer_parameter and iss != pending.issuer
        ):
            raise MCPHubError("mcp_oauth_issuer_mismatch")
        service = pending.service
        provider = CATALOG[service]
        assert self._base is not None
        data = asyncio.run(
            self._token_request(
                provider,
                pending.token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": pending.verifier,
                    "redirect_uri": self._base + "/api/connect/mcp/callback",
                    "client_id": pending.client_id,
                    "resource": provider.endpoint,
                },
            )
        )
        token, refresh, expires = self._parse_token(data)
        try:
            observed = asyncio.run(
                discover_tools(MCPConnection(service, provider.endpoint, True, token))
            )
        except Exception:
            raise MCPHubError("mcp_discovery_failed") from None
        reviewed = tuple(
            entry for tool in observed if (entry := _reviewable(tool, service=service)) is not None
        )
        reviewed = tuple(
            sorted(
                reviewed,
                key=lambda entry: (not entry.observation.read_only_hint, entry.observation.name),
            )
        )[:_MAX_TOOLS]
        if not reviewed:
            raise MCPHubError("mcp_no_compatible_tools")
        with self._lock:
            generation = self._generations.get(service, 0) + 1
            self._generations[service] = generation
            record = _Connection(
                token,
                refresh,
                expires,
                pending.issuer,
                pending.client_id,
                pending.token_endpoint,
                reviewed,
                generation,
            )
            self._connections[service] = record
            self._save(service, record)
        return service

    def _save(self, service: str, record: _Connection) -> None:
        self._vault.save(
            service,
            {
                "token": record.token,
                "refresh_token": record.refresh_token,
                "expires_at": record.expires_at,
                "issuer": record.issuer,
                "client_id": record.client_id,
                "token_endpoint": record.token_endpoint,
                "tools": [
                    {
                        "name": x.observation.name,
                        "schema": x.observation.schema_json,
                        "hash": x.observation.schema_sha256,
                        "read_only": x.observation.read_only_hint,
                    }
                    for x in record.tools
                ],
            },
        )

    def _load_connections(self) -> None:
        # A loaded, authenticated schema is still revalidated against the live
        # MCP server before the NEXT execution. Vault data is never authoritative.
        for service, provider in CATALOG.items():
            try:
                item = self._vault.load(service)
                if not item:
                    continue
                token = item.get("token")
                if not isinstance(token, str) or not 12 <= len(token) <= 8192:
                    continue
                issuer = item.get("issuer")
                token_endpoint = item.get("token_endpoint")
                client_id = item.get("client_id")
                if (
                    not isinstance(issuer, str)
                    or not _trusted_url(issuer, provider)
                    or not isinstance(token_endpoint, str)
                    or not _trusted_url(token_endpoint, provider)
                    or not isinstance(client_id, str)
                ):
                    continue
                tools: list[_RemoteTool] = []
                for raw in item.get("tools", [])[:_MAX_TOOLS]:
                    schema = raw["schema"]
                    if (
                        not isinstance(schema, str)
                        or hashlib.sha256(schema.encode()).hexdigest() != raw["hash"]
                    ):
                        continue
                    observation = ObservedTool(
                        service, raw["name"], schema, raw["hash"], raw["read_only"]
                    )
                    reviewed = _reviewable(observation, service=service)
                    if reviewed is not None:
                        tools.append(reviewed)
                if tools:
                    self._generations[service] = 1
                    self._connections[service] = _Connection(
                        token,
                        item.get("refresh_token"),
                        item.get("expires_at"),
                        issuer,
                        client_id,
                        token_endpoint,
                        tuple(tools),
                        1,
                    )
            except (ValueError, KeyError, TypeError, IndexError):
                continue

    def connect_custom(
        self, alias: str, endpoint: str, *, bearer_token: str | None = None
    ) -> dict[str, Any]:
        """Connect any HTTPS MCP server by URL and discover its reviewed tools."""
        clean_alias = alias.strip().lower()
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,29}", clean_alias)
            or clean_alias == "github"
            or clean_alias in CATALOG
        ):
            raise ToolError("invalid_mcp_alias")
        clean_url = endpoint.strip()
        if not clean_url.startswith("https://"):
            raise ToolError("invalid_mcp_endpoint")
        token = (bearer_token or "").strip()
        try:
            connection = MCPConnection(
                connection_id=clean_alias,
                endpoint=clean_url,
                discovery_approved=True,
                bearer_token=token or None,
            )
            outcome: list[tuple[bool, tuple[ObservedTool, ...] | BaseException]] = []

            def _discover() -> None:
                try:
                    outcome.append((True, asyncio.run(discover_tools(connection))))
                except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
                    outcome.append((False, exc))

            worker = threading.Thread(target=_discover, name="max-mcp-discover", daemon=True)
            worker.start()
            worker.join(timeout=35)
            if not outcome:
                raise ToolError("mcp_discovery_timeout")
            ok, payload = outcome[0]
            if not ok:
                assert isinstance(payload, BaseException)
                raise payload
            assert not isinstance(payload, BaseException)
            observed = payload
        except ToolError:
            raise
        except Exception:
            raise ToolError("mcp_discovery_failed") from None
        tools = []
        for entry in observed:
            reviewed = _reviewable(entry, service=clean_alias)
            if reviewed is not None:
                tools.append(reviewed)
        if not tools:
            raise ToolError("no_supported_mcp_tools")
        with self._lock:
            generation = self._generations.get(clean_alias, 0) + 1
            self._generations[clean_alias] = generation
            self._custom_endpoints[clean_alias] = clean_url
            self._connections[clean_alias] = _Connection(
                token,
                None,
                None,
                clean_url,
                "",
                "",
                tuple(tools),
                generation,
            )
        return {
            "connected": clean_alias,
            "endpoint": clean_url,
            "tools": [t.observation.name for t in tools],
        }

    def disconnect(self, service: str) -> None:
        if service == "github":
            self._github.disconnect()
            return
        with self._lock:
            is_custom = service in self._custom_endpoints or service in self._connections
            if service not in CATALOG and not is_custom:
                raise MCPHubError("unknown_mcp_service")
            self._connections.pop(service, None)
            self._custom_endpoints.pop(service, None)
            self._generations[service] = self._generations.get(service, 0) + 1
            self._pending = {
                key: value for key, value in self._pending.items() if value.service != service
            }
            if service in CATALOG:
                self._vault.delete(service)

    def _list_connected(self, _: Mapping[str, Any]) -> str:
        with self._lock:
            snapshot = tuple(self._connections.items())
            custom = dict(self._custom_endpoints)
        services: dict[str, Any] = {
            "github": {
                "connected": self._github.status().get("connected", False),
                "tools": [t.name for t in self._github.tools_for_session()],
            }
        }
        for service, conn in snapshot:
            services[service] = {
                "connected": True,
                "endpoint": custom.get(service)
                or (CATALOG[service].endpoint if service in CATALOG else ""),
                "tools": [f"mcp.{service}.{entry.observation.name}" for entry in conn.tools],
            }
        return json.dumps({"services": services}, sort_keys=True)

    def _connect_tool(self, arguments: Mapping[str, Any]) -> str:
        alias = str(arguments["alias"])
        url = str(arguments["url"])
        token = arguments.get("bearer_token")
        result = self.connect_custom(
            alias,
            url,
            bearer_token=str(token) if token is not None else None,
        )
        return json.dumps(result, sort_keys=True)

    def _disconnect_tool(self, arguments: Mapping[str, Any]) -> str:
        alias = str(arguments["alias"]).strip().lower()
        try:
            self.disconnect(alias)
        except MCPHubError as exc:
            raise ToolError(exc.code) from None
        return json.dumps({"disconnected": alias}, sort_keys=True)

    def tools_for_session(self) -> tuple[Tool, ...]:
        # GitHub has a finite host-owned catalogue. Keep the existing separate
        # budget for other providers rather than letting them crowd out GitHub.
        github_tools = self._github.tools_for_session()
        management_tools = (
            Tool(
                name="mcp.list",
                description="List all connected MCP servers and their available remote tools.",
                risk=ToolRisk.READ_ONLY,
                params=(),
                handler=self._list_connected,
            ),
            Tool(
                name="mcp.connect",
                description=(
                    "Connect any remote HTTPS MCP server by URL and discover its tools "
                    "(owner approval required)."
                ),
                risk=ToolRisk.EFFECTFUL,
                params=(
                    ToolParam(
                        "alias", str, "Short lowercase server alias, e.g. 'linear'", max_length=30
                    ),
                    ToolParam("url", str, "HTTPS MCP endpoint URL", max_length=500),
                    ToolParam(
                        "bearer_token",
                        str,
                        "Optional bearer token if required by the MCP server",
                        required=False,
                        max_length=2000,
                    ),
                ),
                handler=self._connect_tool,
            ),
            Tool(
                name="mcp.disconnect",
                description="Disconnect a connected MCP server by alias (owner approval required).",
                risk=ToolRisk.EFFECTFUL,
                params=(
                    ToolParam(
                        "alias", str, "Connected MCP server alias to disconnect", max_length=30
                    ),
                ),
                handler=self._disconnect_tool,
            ),
        )
        all_tools: list[Tool] = []
        with self._lock:
            snapshot = tuple(self._connections.items())
        for service, connection in snapshot:
            label = CATALOG[service].name if service in CATALOG else service
            for entry in connection.tools:
                raw_tool = re.sub(r"[^a-z0-9_]", "_", entry.observation.name.lower()).strip("_")
                if not raw_tool or not raw_tool[0].isalpha():
                    raw_tool = f"tool_{raw_tool}" if raw_tool else "tool"
                name = "mcp." + service + "." + raw_tool[:31]

                def handler(
                    arguments: Mapping[str, Any],
                    *,
                    selected_service: str = service,
                    selected_entry: _RemoteTool = entry,
                    generation: int = connection.generation,
                ) -> str:
                    return self._call(selected_service, selected_entry, dict(arguments), generation)

                all_tools.append(
                    Tool(
                        name=name,
                        description=f"{label} MCP {entry.observation.name} "
                        "(remote; owner approval required for EVERY operation)",
                        risk=ToolRisk.EFFECTFUL,
                        params=entry.params,
                        handler=handler,
                    )
                )
        return (*management_tools, *github_tools, *all_tools[:_MAX_TOOLS])

    def _refresh(self, service: str, record: _Connection) -> _Connection:
        if not record.token_endpoint or service not in CATALOG:
            return record
        if record.expires_at is None or record.expires_at > time.time() + 60:
            return record
        if not record.refresh_token:
            raise ToolError("mcp_oauth_expired")
        data = asyncio.run(
            self._token_request(
                CATALOG[service],
                record.token_endpoint,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": record.refresh_token,
                    "client_id": record.client_id,
                    "resource": CATALOG[service].endpoint,
                },
            )
        )
        token, refreshed, deadline = self._parse_token(data)
        next_record = _Connection(
            token,
            refreshed or record.refresh_token,
            deadline,
            record.issuer,
            record.client_id,
            record.token_endpoint,
            record.tools,
            record.generation,
        )
        with self._lock:
            if self._connections.get(service) != record:
                raise ToolError("mcp_connection_revoked")
            self._connections[service] = next_record
            self._save(service, next_record)
        return next_record

    def _call(
        self, service: str, entry: _RemoteTool, arguments: dict[str, Any], generation: int
    ) -> str:
        with self._lock:
            record = self._connections.get(service)
            if record is None or record.generation != generation or entry not in record.tools:
                raise ToolError("mcp_connection_revoked")
        try:
            validated = validated_arguments(entry.observation, entry.policy, arguments)
            current = self._refresh(service, record)
            return _run_in_fresh_thread(
                asyncio.run,
                self._dispatch(service, current.token, entry, validated, generation),
            )
        except ToolError:
            raise
        except Exception:
            raise ToolError("mcp_remote_call_failed") from None

    async def _dispatch(
        self,
        service: str,
        token: str,
        entry: _RemoteTool,
        args: dict[str, Any],
        generation: int,
    ) -> str:
        with self._lock:
            endpoint = self._custom_endpoints.get(service) or CATALOG[service].endpoint
        headers = {"Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with anyio.fail_after(30):
                async with httpx.AsyncClient(
                    transport=EndpointTransport(endpoint, httpx.AsyncHTTPTransport(retries=0)),
                    headers=headers,
                    timeout=10,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with streamable_http_client(endpoint, http_client=client) as (
                        reader,
                        writer,
                        _,
                    ):
                        async with ClientSession(
                            reader, writer, read_timeout_seconds=timedelta(seconds=10)
                        ) as mcp:
                            initialized = await mcp.initialize()
                            if initialized.capabilities.tools is None:
                                raise ToolError("mcp_schema_drifted")
                            found = False
                            cursor: str | None = None
                            for _ in range(16):
                                page = await mcp.list_tools(cursor=cursor)
                                for tool in page.tools:
                                    if tool.name == entry.observation.name:
                                        schema = json.dumps(
                                            tool.inputSchema,
                                            sort_keys=True,
                                            separators=(",", ":"),
                                            allow_nan=False,
                                        )
                                        if (
                                            schema != entry.observation.schema_json
                                            or bool(
                                                getattr(tool.annotations, "readOnlyHint", False)
                                            )
                                            != entry.observation.read_only_hint
                                        ):
                                            raise ToolError("mcp_schema_drifted")
                                        found = True
                                cursor = page.nextCursor
                                if cursor is None:
                                    break
                            if not found:
                                raise ToolError("mcp_schema_drifted")
                            with self._lock:
                                record = self._connections.get(service)
                                if (
                                    record is None
                                    or record.generation != generation
                                    or entry not in record.tools
                                ):
                                    raise ToolError("mcp_connection_revoked")
                            result = await mcp.call_tool(entry.observation.name, arguments=args)
                            return _extract_tool_output(result)
        except ToolError:
            raise
        except Exception:
            raise ToolError("mcp_remote_call_failed") from None
