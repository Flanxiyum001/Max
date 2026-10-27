"""Browser research guardrails (optional Playwright MCP provider).

Everything here is a *policy* layer that runs before any navigation: URL
scheme/host/port validation, private-network and metadata-IP blocking, and
an allowlist. The Playwright MCP server itself is only ever started as a
subprocess through the MCP manager, and read-only research is the default.

If Playwright or its browser binaries are absent, ``browser_*`` tools report
an actionable diagnostic and text chat keeps working.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from ..core.tools import ToolError

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {"80", "443", ""}
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "instance-data",
    "0.0.0.0",
    "::1",
}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain")
BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


@dataclass(frozen=True)
class URLVerdict:
    allowed: bool
    reason: str
    normalized: str = ""


def check_url(url: str, allow_hosts: tuple[str, ...] = (), resolve: bool = True) -> URLVerdict:
    """Validate a navigation target. Fail closed on anything ambiguous."""
    raw = str(url or "").strip()
    if not raw:
        return URLVerdict(False, "empty url")
    if len(raw) > 2048:
        return URLVerdict(False, "url too long")
    if any(ch in raw for ch in ("\\", "\n", "\t", " ")):
        return URLVerdict(False, "url contains control or whitespace characters")

    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return URLVerdict(False, f"scheme '{parts.scheme or '(none)'}' is not permitted")
    if parts.username or parts.password:
        return URLVerdict(False, "credentials embedded in url are not permitted")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return URLVerdict(False, "url has no host")
    if parts.port is not None and str(parts.port) not in ALLOWED_PORTS:
        return URLVerdict(False, f"port {parts.port} is not permitted")

    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        return URLVerdict(False, f"host '{host}' is a local or metadata address")
    if _is_blocked_ip(host):
        return URLVerdict(False, f"host '{host}' resolves to a private/reserved address")

    if allow_hosts:
        if not _host_allowed(host, allow_hosts):
            return URLVerdict(False, f"host '{host}' is not in the browser host allowlist")

    if resolve and not _is_ip(host):
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return URLVerdict(False, f"host '{host}' could not be resolved")
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                return URLVerdict(False, "resolved to an unparseable address")
            if _is_blocked_ip(str(addr)):
                return URLVerdict(False, f"'{host}' resolves to a private address ({addr})")
            if addr.is_link_local or addr.is_multicast or addr.is_reserved:
                return URLVerdict(False, f"'{host}' resolves to a non-public address ({addr})")

    normalized = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))
    return URLVerdict(True, "allowed", normalized)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_blocked_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in BLOCKED_NETWORKS) or addr.is_private or addr.is_loopback


def _host_allowed(host: str, allow_hosts: tuple[str, ...]) -> bool:
    for entry in allow_hosts:
        entry = entry.lower().strip().lstrip("*.")
        if host == entry or host.endswith("." + entry):
            return True
    return False


class BrowserProvider:
    """Read-only-by-default browser research behind the MCP manager.

    ``manager`` is an :class:`~max.mcp.backend.MCPServerManager`; the browser
    server must be configured separately with id ``playwright``.
    """

    def __init__(self, manager, enabled: bool = False, allow_hosts: tuple[str, ...] = (), audit=None):
        self.manager = manager
        self.enabled = enabled
        self.allow_hosts = tuple(allow_hosts)
        self.audit = audit
        self.server_id = "playwright"

    def _require(self) -> None:
        if not self.enabled:
            raise ToolError(
                "browser integration is disabled — set MAX_BROWSER_ENABLE=1 and configure "
                "MAX_BROWSER_ALLOW_HOSTS (optional) to enable it"
            )
        if self.server_id not in self.manager.configs:
            raise ToolError(
                "no Playwright MCP server is configured — add it to MAX_MCP_SERVERS with "
                "id 'playwright' and enabled=true"
            )

    def _guard(self, url: str) -> str:
        verdict = check_url(url, self.allow_hosts)
        if self.audit is not None:
            self.audit.record(
                "browser_navigation",
                url=_host_of(url),
                allowed=verdict.allowed,
                reason=verdict.reason,
            )
        if not verdict.allowed:
            raise ToolError(f"navigation blocked: {verdict.reason}")
        return verdict.normalized

    def open(self, url: str) -> str:
        self._require()
        target = self._guard(url)
        return self.manager.call(self.server_id, "browser_open", {"url": target})

    def extract(self, url: str | None = None) -> str:
        self._require()
        target = self._guard(url) if url else None
        args = {"url": target} if target else {}
        return self.manager.call(self.server_id, "browser_extract", args)

    def find(self, query: str, url: str | None = None) -> str:
        self._require()
        target = self._guard(url) if url else None
        args = {"query": str(query)[:200]}
        if target:
            args["url"] = target
        return self.manager.call(self.server_id, "browser_find", args)

    def close(self) -> str:
        self._require()
        return self.manager.call(self.server_id, "browser_close_session", {})


def _host_of(url: str) -> str:
    try:
        return (urlsplit(str(url)).hostname or "")[:120]
    except ValueError:
        return ""


def register_browser_tools(registry, provider: BrowserProvider) -> None:
    from ..core.tools import ToolContext, ToolSpec

    def _open(args: dict, ctx: ToolContext) -> str:
        return provider.open(args.get("url", ""))

    def _extract(args: dict, ctx: ToolContext) -> str:
        return provider.extract(args.get("url"))

    def _find(args: dict, ctx: ToolContext) -> str:
        return provider.find(args.get("query", ""), args.get("url"))

    def _close(args: dict, ctx: ToolContext) -> str:
        return provider.close()

    registry.register(
        ToolSpec(
            name="browser_open",
            description="Navigate to a public documentation URL (read-only; blocked hosts are rejected).",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            category="net",
            handler=_open,
        )
    )
    registry.register(
        ToolSpec(
            name="browser_extract",
            description="Extract readable text/headings/links from a public page or the current page.",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}},
            category="net",
            handler=_extract,
        )
    )
    registry.register(
        ToolSpec(
            name="browser_find",
            description="Find text on a public page and return bounded snippets with source URLs.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}, "url": {"type": "string"}},
                "required": ["query"],
            },
            category="net",
            handler=_find,
        )
    )
    registry.register(
        ToolSpec(
            name="browser_close_session",
            description="Close the disposable browser context (drops cookies, storage and sessions).",
            parameters={"type": "object", "properties": {}},
            category="net",
            handler=_close,
        )
    )
