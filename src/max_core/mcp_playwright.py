"""Microsoft Playwright MCP (@playwright/mcp) integration for MAX.

Provides a safe, allowlisted stdio MCP connection for Microsoft's official
Playwright MCP server (https://github.com/microsoft/playwright-mcp), using an
isolated browser profile (``--isolated``) and fail-closed MAX Trust risk
classification (read-only inspection vs owner-approved browser actions).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from max_core.mcp_desktop import MCPDesktopManager
from max_core.mcp_policy import RiskTier
from max_core.mcp_stdio import StdioMCPConnection, allowlisted_stdio_connection

PlaywrightBrowser = Literal["chromium", "chrome", "msedge", "firefox", "webkit"]
_ALLOWED_BROWSERS = frozenset({"chromium", "chrome", "msedge", "firefox", "webkit"})
_PACKAGE_RE = re.compile(r"^@playwright/mcp(?:@[A-Za-z0-9._-]+)?$")

# Read-only inspection tools from @playwright/mcp; everything else is EFFECTFUL.
_READ_ONLY_TOOLS = frozenset(
    {
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_wait_for",
        "browser_tab_list",
    }
)


class PlaywrightMCPError(ValueError):
    """Fixed safe error code for Playwright MCP setup/validation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _validate_origin(raw: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip() or any(c.isspace() for c in raw):
        raise PlaywrightMCPError("invalid_playwright_origin")
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise PlaywrightMCPError("invalid_playwright_origin") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if (
        scheme not in {"https", "http"}
        or not host
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or (scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise PlaywrightMCPError("invalid_playwright_origin")
    return f"{scheme}://{parts.netloc}"


@dataclass(frozen=True, slots=True)
class PlaywrightMCPConfig:
    """Validated host configuration for ``@playwright/mcp``."""

    browser: PlaywrightBrowser = "chromium"
    headless: bool = True
    isolated: bool = True
    allowed_origins: tuple[str, ...] = ()
    blocked_origins: tuple[str, ...] = ()
    package: str = "@playwright/mcp@latest"

    def __post_init__(self) -> None:
        if self.browser not in _ALLOWED_BROWSERS:
            raise PlaywrightMCPError("invalid_playwright_browser")
        if type(self.headless) is not bool or type(self.isolated) is not bool:
            raise PlaywrightMCPError("invalid_playwright_config")
        if not isinstance(self.package, str) or not _PACKAGE_RE.fullmatch(self.package):
            raise PlaywrightMCPError("invalid_playwright_package")
        if len(self.allowed_origins) > 10 or len(self.blocked_origins) > 10:
            raise PlaywrightMCPError("too_many_playwright_origins")
        for origin in self.allowed_origins + self.blocked_origins:
            _validate_origin(origin)

    def to_npx_args(self) -> tuple[str, ...]:
        """Build bounded CLI arguments (<= 8) for ``npx``."""
        args: list[str] = [
            "-y",
            self.package,
            f"--browser={self.browser}",
        ]
        if self.isolated:
            args.append("--isolated")
        if self.headless:
            args.append("--headless")
        if self.allowed_origins:
            cleaned = ";".join(_validate_origin(o) for o in self.allowed_origins)
            args.append(f"--allowed-origins={cleaned}")
        if self.blocked_origins:
            cleaned = ";".join(_validate_origin(o) for o in self.blocked_origins)
            args.append(f"--blocked-origins={cleaned}")
        return tuple(args)


def resolve_playwright_binary(env: Mapping[str, str] | None = None) -> str:
    """Resolve the host ``npx`` or pinned ``MAX_PLAYWRIGHT_MCP_BIN`` absolute path."""
    source = os.environ if env is None else env
    override = source.get("MAX_PLAYWRIGHT_MCP_BIN", "").strip()
    if override:
        if not os.path.isabs(override) or ".." in override.replace("\\", "/").split("/"):
            raise PlaywrightMCPError("invalid_playwright_binary")
        if not Path(override).is_file():
            raise PlaywrightMCPError("playwright_binary_not_found")
        return str(Path(override).resolve())

    found = shutil.which("npx")
    if not found:
        raise PlaywrightMCPError("playwright_npx_not_found")
    return str(Path(found).resolve())


def build_playwright_connection(
    *,
    config: PlaywrightMCPConfig | None = None,
    binary: str | None = None,
    allowed_commands: Sequence[str] | None = None,
    discovery_approved: bool = False,
    env: Mapping[str, str] | None = None,
) -> StdioMCPConnection:
    """Create an allowlisted ``StdioMCPConnection`` for Playwright MCP."""
    cfg = config if config is not None else PlaywrightMCPConfig()
    resolved_bin = binary if binary is not None else resolve_playwright_binary(env)
    allowlist = tuple(allowed_commands) if allowed_commands is not None else (resolved_bin,)
    return allowlisted_stdio_connection(
        "playwright",
        resolved_bin,
        cfg.to_npx_args(),
        allowed_commands=allowlist,
        discovery_approved=discovery_approved,
    )


def playwright_tool_risk(tool_name: str, *, read_only_hint: bool = False) -> RiskTier:
    """Fail-closed risk classification for ``@playwright/mcp`` tools."""
    if tool_name in {"browser_evaluate", "browser_file_upload"}:
        return RiskTier.R3
    if tool_name in _READ_ONLY_TOOLS and (
        read_only_hint or not tool_name.startswith("browser_evaluate")
    ):
        return RiskTier.R0
    return RiskTier.R2


def register_playwright_mcp(
    manager: MCPDesktopManager,
    *,
    config: PlaywrightMCPConfig | None = None,
    binary: str | None = None,
    discovery_approved: bool = False,
    env: Mapping[str, str] | None = None,
) -> StdioMCPConnection:
    """Register the Playwright MCP stdio server into ``MCPDesktopManager``."""
    cfg = config if config is not None else PlaywrightMCPConfig()
    resolved_bin = binary if binary is not None else resolve_playwright_binary(env)
    args = cfg.to_npx_args()
    manager.add_stdio(
        "playwright",
        resolved_bin,
        args,
        discovery_approved=discovery_approved,
    )
    return StdioMCPConnection(
        "playwright",
        resolved_bin,
        args,
        discovery_approved=discovery_approved,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--register" in args:
        manager = MCPDesktopManager()
        try:
            conn = register_playwright_mcp(manager, discovery_approved=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": getattr(exc, "code", str(exc))}))
            return 1
        print(
            json.dumps({"ok": True, "connection_id": conn.connection_id, "command": conn.command})
        )
        return 0

    try:
        binary = resolve_playwright_binary()
    except PlaywrightMCPError as exc:
        print(json.dumps({"ok": False, "available": False, "error": exc.code}))
        return 1
    cfg = PlaywrightMCPConfig()
    print(
        json.dumps(
            {
                "ok": True,
                "available": True,
                "command": binary,
                "args": list(cfg.to_npx_args()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
