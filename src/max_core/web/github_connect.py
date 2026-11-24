"""MAX web orb: browser-authorized GitHub MCP connector.

OAuth app registration is a one-time host configuration. The owner starts the
flow with a command and authorizes at github.com; MAX owns the endpoint, state,
discovery, and approval-gated tool execution. No token is sent to the browser,
model, logs, or disk. Render's ephemeral process loses its connection on restart.
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
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from max_core.github_tool_catalogue import (
    GITHUB_MCP_ENDPOINT,
    GITHUB_READS,
    GITHUB_TOOLS,
    GITHUB_TOOL_GUIDANCE,
    GitHubSchemaError,
    compact_schema,
    github_headers,
    reviewed_github_schema,
    validate_github_arguments,
    validate_github_operation,
)
from max_core.mcp_discovery import MCPConnection, ObservedTool, discover_tools
from max_core.mcp_execute import _extract_tool_output, _run_in_fresh_thread
from max_core.mcp_policy import ReviewedToolPolicy, RiskTier, validated_arguments
from max_core.mcp_transport import EndpointTransport
from max_core.tools import Tool, ToolError, ToolParam, ToolRisk

_REMOTE_ENDPOINT = GITHUB_MCP_ENDPOINT
_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,30}\Z")
_STATE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_SCALAR_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}
_PENDING_SECONDS = 600

_CONNECT = re.compile(
    r"\b(?:connect|link|integrate|set\s*up|setup|sign\s*in\s*to|log\s*in\s*to)"
    r"\b[\s\S]{0,65}?\bgithub\b",
    re.IGNORECASE,
)
_DISCONNECT = re.compile(r"\b(?:disconnect|unlink|revoke)\b[\s\S]{0,65}?\bgithub\b", re.I)


def github_connect_intent(text: str) -> str | None:
    """Deterministic host routing: never let model output authorize a service."""
    if _DISCONNECT.search(text):
        return "disconnect"
    if _CONNECT.search(text):
        return "connect"
    return None


class GitHubConnectionError(ValueError):
    """Safe fixed error codes; never carry GitHub tokens or provider bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Pending:
    verifier: str
    created: float


@dataclass(frozen=True)
class _RemoteTool:
    observation: ObservedTool
    policy: ReviewedToolPolicy
    params: tuple[ToolParam, ...]
    input_schema: dict[str, Any] | None = None


def _callback_ok(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == "/api/connect/github/callback"
        and parsed.port in (None, 443)
        and len(value) <= 1024
    )


def _reviewable(observation: ObservedTool, *, service: str = "github") -> _RemoteTool | None:
    """GitHub has a fixed host allowlist; other providers retain scalar review."""
    if observation.connection_id != service:
        return None
    if service == "github":
        if observation.name not in GITHUB_TOOLS:
            return None
        try:
            schema = reviewed_github_schema(observation.schema_json, observation.schema_sha256)
            policy = ReviewedToolPolicy(
                "owner",
                service,
                observation.name,
                observation.schema_sha256,
                RiskTier.R1 if observation.name in GITHUB_READS else RiskTier.R2,
                frozenset(schema.get("properties", {})),
                True,
            )
            return _RemoteTool(observation, policy, (), compact_schema(schema))
        except (ValueError, TypeError, KeyError, RecursionError):
            return None
    if not _TOOL_NAME.fullmatch(observation.name):
        return None
    try:
        schema = json.loads(observation.schema_json)
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(props, dict)
            or not isinstance(required, list)
            or len(props) > 12
            or not set(required) <= set(props)
            or any(not _TOOL_NAME.fullmatch(name) for name in props)
        ):
            return None
        params = []
        for name, descriptor in props.items():
            if not isinstance(descriptor, dict):
                return None
            kind = descriptor.get("type")
            if not isinstance(kind, str):
                return None
            param_type = _SCALAR_TYPES.get(kind)
            if param_type is None:
                return None
            params.append(
                ToolParam(
                    name=name,
                    kind=param_type,
                    description=f"MCP parameter {name}",
                    required=name in required,
                    max_length=1024,
                )
            )
        policy = ReviewedToolPolicy(
            "owner",
            service,
            observation.name,
            observation.schema_sha256,
            RiskTier.R1 if observation.read_only_hint else RiskTier.R2,
            frozenset(props),
            True,
        )
        validated_arguments(observation, policy, {}) if not required else None
        return _RemoteTool(observation, policy, tuple(params))
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


class WebGitHubConnector:
    """A single-owner, in-memory OAuth and read/write approval-gated tool bridge."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._client_id = env.get("MAX_GITHUB_OAUTH_CLIENT_ID", "").strip()
        self._secret = env.get("MAX_GITHUB_OAUTH_CLIENT_SECRET", "")
        self._callback = env.get("MAX_GITHUB_OAUTH_CALLBACK_URL", "").strip()
        # OAuth App 'repo' scope grants broad private-repository access.
        # This is explained before authorization; each actual tool still needs approval.
        self._scope = "repo read:user"
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._token: str | None = None
        self._account: str | None = None
        self._tools: tuple[_RemoteTool, ...] = ()
        self._generation = 0

    @property
    def configured(self) -> bool:
        return (
            _CLIENT_ID.fullmatch(self._client_id) is not None
            and bool(self._secret)
            and _callback_ok(self._callback)
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": self.configured,
                "connected": bool(self._token),
                "account": self._account,
                "tools": len(self._tools),
                "requested_tools": len(GITHUB_TOOLS),
                "available_tools": sorted(entry.observation.name for entry in self._tools),
                "unavailable_tools": sorted(
                    GITHUB_TOOLS - {entry.observation.name for entry in self._tools}
                ),
                "ephemeral": True,
            }

    def start(self) -> str:
        if not self.configured:
            raise GitHubConnectionError("github_oauth_not_configured")
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
                if time.monotonic() - item.created <= _PENDING_SECONDS
            }
            self._pending[state] = _Pending(verifier, time.monotonic())
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._callback,
                "scope": self._scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return _AUTHORIZE_URL + "?" + query

    def _consume(self, state: str) -> _Pending:
        if not _STATE.fullmatch(state):
            raise GitHubConnectionError("github_oauth_state_invalid")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.monotonic() - pending.created > _PENDING_SECONDS:
            raise GitHubConnectionError("github_oauth_state_invalid")
        return pending

    async def _exchange(self, code: str, verifier: str) -> tuple[str, str]:
        """Allowlisted GitHub hosts only, bounded responses and no redirects."""
        try:
            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._secret,
                        "code": code,
                        "redirect_uri": self._callback,
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
                if response.status_code != 200 or len(response.content) > 16_384:
                    raise GitHubConnectionError("github_oauth_exchange_failed")
                raw = response.json()
                token = raw.get("access_token") if isinstance(raw, dict) else None
                if (
                    not isinstance(token, str)
                    or not 12 <= len(token) <= 8192
                    or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)
                ):
                    raise GitHubConnectionError("github_oauth_exchange_failed")
                identity = await client.get(
                    _USER_URL,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                    },
                )
                if identity.status_code != 200 or len(identity.content) > 16_384:
                    raise GitHubConnectionError("github_oauth_identity_failed")
                user = identity.json()
                login = user.get("login") if isinstance(user, dict) else None
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
                    raise GitHubConnectionError("github_oauth_identity_failed")
                return token, login
        except GitHubConnectionError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise GitHubConnectionError("github_oauth_exchange_failed") from None

    def finish(self, state: str, code: str) -> str:
        """Finish one state-bound OAuth grant, discover tools and activate atomically."""
        pending = self._consume(state)
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise GitHubConnectionError("github_oauth_code_invalid")
        token, account = asyncio.run(self._exchange(code, pending.verifier))
        connection = MCPConnection("github", _REMOTE_ENDPOINT, True, token, github_catalogue=True)
        try:
            observed = asyncio.run(discover_tools(connection))
        except Exception:
            raise GitHubConnectionError("github_mcp_discovery_failed") from None
        reviewable: list[_RemoteTool] = []
        for tool in observed:
            entry = _reviewable(tool)
            if entry is not None:
                reviewable.append(entry)
        if not reviewable:
            raise GitHubConnectionError("github_mcp_no_supported_tools")
        with self._lock:
            self._token = token
            self._account = account
            # Never let a remote annotation promote a write to a trusted read.
            # All operations, including reads, still require exact-call consent.
            reviewable.sort(key=lambda candidate: candidate.observation.name)
            self._tools = tuple(reviewable)
            self._generation += 1
        return account

    def disconnect(self) -> None:
        with self._lock:
            self._pending.clear()
            self._token = None
            self._account = None
            self._tools = ()
            self._generation += 1

    def tools_for_session(self) -> tuple[Tool, ...]:
        with self._lock:
            entries = self._tools
            generation = self._generation
        result: list[Tool] = []
        for entry in entries:
            name = "mcp.github." + entry.observation.name

            def handler(
                arguments: Mapping[str, Any],
                *,
                selected: _RemoteTool = entry,
                version: int = generation,
            ) -> str:
                return self._call(selected, dict(arguments), version)

            def validate(arguments: object, *, selected: _RemoteTool = entry) -> dict[str, Any]:
                try:
                    clean = validate_github_arguments(
                        selected.observation.schema_json,
                        selected.observation.schema_sha256,
                        arguments,
                    )
                    validate_github_operation(selected.observation.name, clean)
                    return clean
                except GitHubSchemaError:
                    raise ToolError("github_invalid_arguments") from None

            result.append(
                Tool(
                    name=name,
                    description=(
                        "GitHub MCP: "
                        + GITHUB_TOOL_GUIDANCE[entry.observation.name]
                        + (
                            " (read; requires approval)"
                            if entry.observation.name in GITHUB_READS
                            else " (write; requires approval)"
                        )
                    ),
                    risk=ToolRisk.EFFECTFUL,
                    params=entry.params,
                    handler=handler,
                    argument_validator=validate,
                    input_schema=entry.input_schema,
                )
            )
        return tuple(result)

    def _call(self, entry: _RemoteTool, arguments: dict[str, Any], generation: int) -> str:
        with self._lock:
            if self._token is None or generation != self._generation or entry not in self._tools:
                raise ToolError("github_mcp_revoked")
            token = self._token
        try:
            clean = validate_github_arguments(
                entry.observation.schema_json, entry.observation.schema_sha256, arguments
            )
            validate_github_operation(entry.observation.name, clean)
            return _run_in_fresh_thread(
                asyncio.run, self._dispatch(token, entry, clean, generation)
            )
        except ToolError:
            raise
        except Exception:
            raise ToolError("github_mcp_call_failed") from None

    async def _dispatch(
        self, token: str, entry: _RemoteTool, args: dict[str, Any], generation: int
    ) -> str:
        try:
            with anyio.fail_after(30):
                headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
                headers.update(github_headers())
                async with httpx.AsyncClient(
                    transport=EndpointTransport(
                        _REMOTE_ENDPOINT, httpx.AsyncHTTPTransport(retries=0)
                    ),
                    headers=headers,
                    follow_redirects=False,
                    timeout=10,
                    trust_env=False,
                ) as client:
                    async with streamable_http_client(_REMOTE_ENDPOINT, http_client=client) as (
                        reader,
                        writer,
                        _,
                    ):
                        async with ClientSession(
                            reader, writer, read_timeout_seconds=timedelta(seconds=10)
                        ) as mcp:
                            initialized = await mcp.initialize()
                            if initialized.capabilities.tools is None:
                                raise ToolError("github_mcp_schema_changed")
                            found = False
                            cursor: str | None = None
                            for _ in range(16):
                                page = await mcp.list_tools(cursor=cursor)
                                for tool in page.tools:
                                    if tool.name == entry.observation.name:
                                        encoded = json.dumps(
                                            tool.inputSchema,
                                            sort_keys=True,
                                            separators=(",", ":"),
                                            allow_nan=False,
                                        )
                                        if (
                                            encoded != entry.observation.schema_json
                                            or bool(
                                                getattr(tool.annotations, "readOnlyHint", False)
                                            )
                                            != entry.observation.read_only_hint
                                        ):
                                            raise ToolError("github_mcp_schema_changed")
                                        found = True
                                cursor = page.nextCursor
                                if cursor is None:
                                    break
                            if not found:
                                raise ToolError("github_mcp_schema_changed")
                            with self._lock:
                                if (
                                    self._token != token
                                    or self._generation != generation
                                    or entry not in self._tools
                                ):
                                    raise ToolError("github_mcp_revoked")
                            output = await mcp.call_tool(entry.observation.name, arguments=args)
                return _extract_tool_output(output).replace(token, "[redacted]")
        except ToolError:
            raise
        except Exception:
            raise ToolError("github_mcp_call_failed") from None
