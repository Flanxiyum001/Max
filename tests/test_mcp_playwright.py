"""Tests for Microsoft Playwright MCP (@playwright/mcp) integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from max_core.mcp_desktop import MCPDesktopManager, MCPSettingsError
from max_core.mcp_playwright import (
    PlaywrightMCPConfig,
    PlaywrightMCPError,
    build_playwright_connection,
    playwright_tool_risk,
    register_playwright_mcp,
    resolve_playwright_binary,
)
from max_core.mcp_policy import RiskTier
from max_core.mcp_stdio import StdioBridgeError


def test_playwright_config_builds_isolated_headless_args() -> None:
    cfg = PlaywrightMCPConfig(
        browser="chromium",
        headless=True,
        isolated=True,
        allowed_origins=("https://example.com", "http://127.0.0.1:8000"),
    )
    args = cfg.to_npx_args()
    assert args[0:2] == ("-y", "@playwright/mcp@latest")
    assert "--browser=chromium" in args
    assert "--isolated" in args
    assert "--headless" in args
    assert "--allowed-origins=https://example.com;http://127.0.0.1:8000" in args
    assert len(args) <= 8


def test_playwright_config_rejects_invalid_browser_package_and_origins() -> None:
    with pytest.raises(PlaywrightMCPError, match="invalid_playwright_browser"):
        PlaywrightMCPConfig(browser="opera")  # type: ignore[arg-type]

    with pytest.raises(PlaywrightMCPError, match="invalid_playwright_package"):
        PlaywrightMCPConfig(package="evil-pkg")

    with pytest.raises(PlaywrightMCPError, match="invalid_playwright_origin"):
        PlaywrightMCPConfig(allowed_origins=("http://insecure.example.com",))

    with pytest.raises(PlaywrightMCPError, match="invalid_playwright_origin"):
        PlaywrightMCPConfig(blocked_origins=("javascript:alert(1)",))


def test_resolve_playwright_binary_respects_override_and_rejects_relative(tmp_path: Path) -> None:
    fake_npx = tmp_path / "npx.cmd"
    fake_npx.write_text("@echo off\n", encoding="utf-8")

    assert resolve_playwright_binary({"MAX_PLAYWRIGHT_MCP_BIN": str(fake_npx)}) == str(
        fake_npx.resolve()
    )
    with pytest.raises(PlaywrightMCPError, match="invalid_playwright_binary"):
        resolve_playwright_binary({"MAX_PLAYWRIGHT_MCP_BIN": "relative/npx"})
    with pytest.raises(PlaywrightMCPError, match="playwright_binary_not_found"):
        resolve_playwright_binary({"MAX_PLAYWRIGHT_MCP_BIN": str(tmp_path / "missing.exe")})


def test_build_playwright_connection_requires_allowlist(tmp_path: Path) -> None:
    fake_npx = tmp_path / "npx"
    fake_npx.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved = str(fake_npx.resolve())

    conn = build_playwright_connection(
        binary=resolved,
        allowed_commands=(resolved,),
        discovery_approved=True,
    )
    assert conn.connection_id == "playwright"
    assert conn.command == resolved
    assert "--isolated" in conn.args

    with pytest.raises(StdioBridgeError, match="stdio_command_not_allowed"):
        build_playwright_connection(
            binary=resolved,
            allowed_commands=("/other/bin",),
        )


def test_playwright_tool_risk_classification() -> None:
    assert playwright_tool_risk("browser_snapshot", read_only_hint=True) == RiskTier.R0
    assert playwright_tool_risk("browser_take_screenshot") == RiskTier.R0
    assert playwright_tool_risk("browser_navigate", read_only_hint=False) == RiskTier.R2
    assert playwright_tool_risk("browser_click") == RiskTier.R2
    assert playwright_tool_risk("browser_type") == RiskTier.R2
    assert playwright_tool_risk("browser_evaluate") == RiskTier.R3
    assert playwright_tool_risk("browser_file_upload") == RiskTier.R3
    assert playwright_tool_risk("unknown_tool") == RiskTier.R2


def test_register_playwright_mcp_persists_into_desktop_manager(tmp_path: Path) -> None:
    fake_npx = tmp_path / "npx"
    fake_npx.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved = str(fake_npx.resolve())
    profile = tmp_path / "mcp-servers.json"

    manager = MCPDesktopManager(
        profile,
        env={"MAX_MCP_STDIO_BINARIES": resolved},
    )
    conn = register_playwright_mcp(manager, binary=resolved, discovery_approved=True)
    assert conn.connection_id == "playwright"

    rows = manager.connections()
    assert len(rows) == 1
    assert rows[0]["id"] == "playwright"
    assert rows[0]["kind"] == "stdio"
    assert rows[0]["command"] == resolved

    # Unallowlisted binary is rejected
    unallowed_manager = MCPDesktopManager(tmp_path / "other.json", env={})
    with pytest.raises(MCPSettingsError, match="stdio_not_allowed"):
        register_playwright_mcp(unallowed_manager, binary=resolved)
