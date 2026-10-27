"""Browser guard: schemes, private networks, metadata, allowlists, policy gating."""

from __future__ import annotations

import pytest

from max.browser import BrowserProvider, check_url
from max.core.tools import ToolError, ToolRegistry, ToolError as TE
from max.browser import register_browser_tools
from max.mcp.backend import MCPServerConfig, MCPServerManager


def test_allows_public_https():
    verdict = check_url("https://docs.python.org/3/", resolve=False)
    assert verdict.allowed, verdict.reason
    assert verdict.normalized.startswith("https://docs.python.org/")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "data:text/html,<script>alert(1)</script>",
        "http://localhost/admin",
        "http://127.0.0.1:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://user:pass@example.com/",
        "http://example.com:22/",
        "http://router.local/",
        "",
    ],
)
def test_blocks_prohibited_destinations(url):
    verdict = check_url(url, resolve=False)
    assert not verdict.allowed, url


def test_host_allowlist_is_enforced():
    assert check_url("https://docs.python.org/", allow_hosts=("python.org",), resolve=False).allowed
    assert not check_url("https://evil.example/", allow_hosts=("python.org",), resolve=False).allowed
    assert not check_url("https://notpython.org/", allow_hosts=("python.org",), resolve=False).allowed


def test_dns_resolution_blocks_localhost_names():
    verdict = check_url("http://localhost/", resolve=True)
    assert not verdict.allowed


class _FakeManager:
    def __init__(self):
        self.configs = {}
        self.calls = []

    def call(self, server_id, tool, args):
        self.calls.append((server_id, tool, args))
        return f"ok:{tool}"


def test_provider_disabled_is_actionable():
    provider = BrowserProvider(_FakeManager(), enabled=False)
    with pytest.raises(ToolError, match="disabled"):
        provider.open("https://docs.python.org/")


def test_provider_requires_configured_server():
    provider = BrowserProvider(_FakeManager(), enabled=True)
    with pytest.raises(ToolError, match="MAX_MCP_SERVERS"):
        provider.open("https://docs.python.org/")


def test_provider_blocks_before_touching_the_browser():
    manager = _FakeManager()
    manager.configs = {"playwright": MCPServerConfig(id="playwright", command=["x"], enabled=True)}
    provider = BrowserProvider(manager, enabled=True)
    with pytest.raises(ToolError, match="navigation blocked"):
        provider.open("http://169.254.169.254/latest/meta-data/")
    assert manager.calls == []  # nothing reached the browser


def test_provider_passes_only_guarded_urls():
    manager = _FakeManager()
    manager.configs = {"playwright": MCPServerConfig(id="playwright", command=["x"], enabled=True)}
    provider = BrowserProvider(manager, enabled=True)
    provider.open("https://docs.python.org/3/library")
    assert manager.calls[0][0] == "playwright"
    assert manager.calls[0][1] == "browser_open"
    assert manager.calls[0][2]["url"].startswith("https://docs.python.org/")


def test_browser_tools_are_category_net_and_denied_by_default():
    registry = ToolRegistry()
    register_browser_tools(registry, BrowserProvider(_FakeManager(), enabled=True))
    assert registry.get("browser_open").category == "net"

    from max.core.tools import AuditLog, PermissionPolicy, ToolContext, ToolExecutor

    class _Audit:
        def record(self, *a, **k):
            return {}

    executor = ToolExecutor(
        registry,
        PermissionPolicy(),
        _Audit(),
        ToolContext(workspace_dir=__import__("pathlib").Path("."), read_roots=[]),
    )
    out, event = executor.dispatch("browser_open", {"url": "https://example.com"})
    assert event["allowed"] is False
    assert out.startswith("REJECTED")
