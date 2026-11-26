"""Pinned, owner-installed Playwright MCP stdio connection for the local desktop."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from max_core.mcp_stdio import StdioMCPConnection

PLAYWRIGHT_MCP_VERSION = "0.0.86"


def install_dir(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    if os.name == "nt":
        return (
            Path(source.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "MAX"
            / "playwright-mcp"
        )
    return (
        Path(source.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        / "max"
        / "playwright-mcp"
    )


def installed_connection(env: Mapping[str, str] | None = None) -> StdioMCPConnection:
    """Resolve only the reviewed package version and entry point; never run npx at call time."""
    root = install_dir(env) / "node_modules" / "@playwright" / "mcp"
    manifest = root / "package.json"
    entry = root / "cli.js"
    node = shutil.which("node")
    if not node or not os.path.isabs(node):
        raise ValueError("playwright_node_missing")
    if root.is_symlink() or manifest.is_symlink() or entry.is_symlink():
        raise ValueError("playwright_install_invalid")
    try:
        package = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            (package.get("name"), package.get("version"))
            != (
                "@playwright/mcp",
                PLAYWRIGHT_MCP_VERSION,
            )
            or not entry.is_file()
            or entry.resolve().parent != root.resolve()
        ):
            raise ValueError("playwright_install_invalid")
    except (OSError, ValueError, TypeError):
        raise ValueError("playwright_install_invalid") from None
    return StdioMCPConnection(
        "playwright",
        str(Path(node).resolve()),
        (str(entry.resolve()), "--headless", "--isolated", "--no-webmcp", "--browser", "chrome"),
        True,
        True,
    )
